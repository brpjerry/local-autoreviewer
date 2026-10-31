# SPDX-License-Identifier: GPL-3.0-or-later
"""local-autoreviewer command line."""

import argparse
import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from . import db, events, leakscan, maintenance, paths, pipeline
from .agents import make_agent
from .config import ConfigError, ConfigWatcher
from .github import GitHub, GitHubError, read_token
from .gitops import Git, GitError
from .runner import make_runner
from .triggers import poll as poll_mod
from .worker import Worker, iso

log = logging.getLogger("local_autoreviewer")
_REF_RE = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(?:#|/pull/)(\d+)/?$")


class CliError(Exception):
    pass


def setup_logging(verbose=False):
    fmt = "%(levelname)s %(message)s" if os.environ.get("JOURNAL_STREAM") else \
        "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt, datefmt="%H:%M:%S")


def parse_ref(ref):
    match = _REF_RE.match(ref.strip())
    if not match:
        raise CliError(f"expected owner/repo#N or a PR URL, got {ref!r}")
    return match.group(1), int(match.group(2))


def auth_files(state):
    home = Path.home()
    return [home / ".claude/.credentials.json", home / ".codex/auth.json", Path(state) / "codex-home/auth.json"]


def resolve_token_owner(github, state):
    conn = db.connect(state / "state.sqlite3")
    try:
        try:
            login = github.viewer_login()
        except GitHubError:
            login = db.kv_get(conn, "token_owner")
            if not login:
                raise
            log.warning("GitHub unreachable; using cached token owner %s", login)
            return login
        with db.transaction(conn):
            db.kv_set(conn, "token_owner", login)
        return login
    finally:
        conn.close()


def build_services(cfg):
    state = paths.state_dir()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = read_token(paths.token_file())
    github = GitHub(token, state / "gh")
    git = Git(os.environ.get("LOCAL_AUTOREVIEWER_GIT_BASE", "https://github.com"), github.env())
    owner = resolve_token_owner(github, state)
    return pipeline.Services(
        config=cfg, state=state, github=github, git=git, runner=make_runner(), token_owner=owner,
        secrets=lambda: [token, *leakscan.collect_secrets(auth_files(state))])


def load_config():
    return config_mod.load(paths.config_file())


# Commands ----------------------------------------------------------------------

def cmd_poll(args):
    svc = build_services(load_config())
    results, failures = poll_mod.poll(svc, dry_run=args.dry_run)
    for repo, produced in results.items():
        for event in produced:
            print(f"{'would ingest' if args.dry_run else 'ingested'}: {repo}#{event.number} {event.action} "
                  f"@{event.head_sha[:12]}")
    if not args.dry_run:
        conn = svc.connect()
        try:
            run_housekeeping = maintenance.due(conn, svc.now())
        finally:
            conn.close()
        if run_housekeeping:
            maintenance.housekeeping(svc)
    return 1 if failures else 0


def cmd_worker(args):
    watcher = ConfigWatcher(paths.config_file(), log)
    base = build_services(watcher.get())
    worker = Worker(lambda cfg: dataclasses.replace(base, config=cfg), watcher)
    worker.install_signals()
    return worker.run()


def _repo_cfg(cfg, repo):
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        raise CliError(f"{repo} is not in {cfg.path}; add a [[repo]] entry first")
    return repo_cfg


def cmd_review(args):
    cfg = load_config()
    repo, number = parse_ref(args.ref)
    repo_cfg = _repo_cfg(cfg, repo)
    svc = build_services(cfg)
    agents = args.agent or list(repo_cfg.agents)
    locks = {}  # job_id -> held lock file; while it is held the worker leaves the job alone
    owned = []  # job ids whose rows committed (a rolled-back id may be reused by another process)
    worst = 0
    try:
        # Like enqueue, read the head and create the jobs under the poll lock, so a poll applying
        # an older snapshot can't supersede them.
        with poll_mod.poll_lock(svc.state):
            pr = svc.github.pr_view(repo_cfg.name, number)
            conn = svc.connect()
            try:
                for agent in agents:
                    with db.transaction(conn):
                        rerun = db.next_rerun(conn, repo_cfg.name, number, pr["headRefOid"], pr["baseRefName"],
                                              agent)
                        job_id = db.insert_job(conn, repo=repo_cfg.name, number=number, head_sha=pr["headRefOid"],
                                               base_ref=pr["baseRefName"], agent=agent,
                                               model=cfg.agents[agent].model, source=db.FOREGROUND,
                                               not_before=svc.now(), now=svc.now(), post=args.post, rerun=rerun)
                        db.update_job(conn, job_id, status="running", started_at=svc.now())
                        locks[job_id] = pipeline.hold_foreground_lock(svc.state, job_id)
                    owned.append(job_id)
            finally:
                conn.close()
        for job_id in owned:
            status = pipeline.run_foreground(svc, job_id, notify=lambda message: print(message, file=sys.stderr))
            conn = svc.connect()
            job = db.get_job(conn, job_id)
            conn.close()
            out = Path(pipeline.job_paths(svc.state, job_id).out, "review.md")
            print(f"job {job_id} [{job['agent']}]: {status} — {job['reason']}")
            if job["review_url"]:
                print(f"  posted: {job['review_url']}")
            elif out.is_file():
                print(f"  review: {out}")
            if status not in ("posted", "skipped"):
                worst = 1
    finally:
        # Never leave this command's jobs for the worker, even after Ctrl-C or an error.
        pipeline.abandon(svc, owned, "foreground review ended before the job finished")
        if owned:
            conn = svc.connect()
            mid_post = [job_id for job_id in owned if db.get_job(conn, job_id)["status"] == "posting"]
            conn.close()
            for job_id in mid_post:
                print(f"job {job_id}'s post didn't complete; the worker, when it runs, will check whether it "
                      "reached GitHub and finish posting it if not", file=sys.stderr)
        for job_id, handle in locks.items():
            pipeline.release_foreground_lock(svc.state, job_id, handle)
    return worst


def cmd_enqueue(args):
    cfg = load_config()
    repo, number = parse_ref(args.ref)
    repo_cfg = _repo_cfg(cfg, repo)
    svc = build_services(cfg)
    # Like a poll, fetch and ingest under the poll lock: a poll applying an older snapshot
    # after this commits would otherwise supersede the newer job queued here.
    with poll_mod.poll_lock(svc.state):
        pr = svc.github.pr_view(repo_cfg.name, number)
        if pr["state"] != "OPEN":
            raise CliError(f"{repo}#{number} is {pr['state'].lower()}")
        event = events.PrEvent(repo=repo_cfg.name, number=number, action="opened", head_sha=pr["headRefOid"],
                               base_ref=pr["baseRefName"], is_draft=pr["isDraft"],
                               author=(pr.get("author") or {}).get("login", ""), source="manual",
                               force=args.force, agents=tuple(args.agent) if args.agent else None)
        conn = svc.connect()
        try:
            with db.transaction(conn):
                events.record(conn, event, svc.now())
                created = events.ingest(conn, cfg, event, now=svc.now(), token_owner=svc.token_owner)
        finally:
            conn.close()
    if created:
        print("queued job(s): " + ", ".join(map(str, created)))
    else:
        print("nothing queued: this head was already reviewed or is in flight (use --force to review again)")
    return 0


def cmd_status(args):
    state = paths.state_dir()
    conn = db.connect(state / "state.sqlite3")
    try:
        active = [dict(r) for r in conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(db.ACTIVE))}) ORDER BY id",
            db.ACTIVE)]
        recent = [dict(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (args.limit,))]
        last_poll = db.kv_get(conn, "last_poll")
    finally:
        conn.close()
    if args.json:
        print(json.dumps({"active": active, "recent": recent, "last_poll": last_poll}, indent=2, default=str))
        return 0
    print(f"last poll: {iso(int(last_poll)) if last_poll else 'never'}")
    print(f"active: {len(active)}")
    for job in recent:
        when = iso(job["finished_at"] or job["started_at"] or job["created_at"])
        line = (f"{job['id']:>5}  {job['status']:<10} {job['agent']:<6} {job['repo']}#{job['number']} "
                f"@{job['head_sha'][:7]}  {when}")
        print(line)
        detail = job["review_url"] or job["reason"]
        if detail:
            print(f"       {detail[:160]}")
    return 0


FINDING_COLUMNS = ("f.*, j.repo, j.number, j.head_sha, j.base_ref, j.agent, j.model, j.effort, j.prompt_version, "
                   "j.source, j.status AS job_status, j.created_at AS job_created_at, j.review_url, j.usage_json, "
                   "j.review_kind, j.followup_of, (SELECT json_group_array(json_object('job_id', r.job_id, 'status', "
                   "r.status, 'detail', r.detail)) FROM resolutions r WHERE r.finding_id=f.id) AS resolutions")


def cmd_findings(args):
    where, params = [], []
    if args.ref:
        repo, number = parse_ref(args.ref)
        where.append("j.repo=? COLLATE NOCASE AND j.number=?")
        params += [repo, number]
    if args.job is not None:
        where.append("f.job_id=?")
        params.append(args.job)
    if args.kind:
        where.append(f"f.kind IN ({','.join('?' * len(args.kind))})")
        params += args.kind
    if args.untriaged:
        where.append("f.disposition IS NULL")
    sql = (f"SELECT {FINDING_COLUMNS} FROM findings f JOIN jobs j ON j.id=f.job_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY f.job_id, f.kind, f.position")
    conn = db.connect(paths.state_dir() / "state.sqlite3")
    try:
        rows = [dict(r) for r in conn.execute(sql, params)]
        for row in rows:  # each follow-up's status for this finding
            row["resolutions"] = json.loads(row["resolutions"] or "[]")
    finally:
        conn.close()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        rating = row["severity"] or row["kind"].replace("_", " ")
        if row["impact"] is not None:
            rating += f" {row['impact']}/10"
        if row["disposition"] or row["rated_impact"] is not None:
            rated = "" if row["rated_impact"] is None else f" {row['rated_impact']}/10"
            rating += f"  [{row['disposition'] or 'rated'}{rated}]"
        location = f"  {row['file']}:{row['line']}" if row["file"] else ""
        print(f"{row['id']:>5}  job {row['job_id']:<4} {row['agent']:<6} {row['repo']}#{row['number']} "
              f"@{row['head_sha'][:7]}  {rating}")
        print(f"       {row['title'][:150]}{location}")
    return 0


def _impact_arg(value):
    impact = int(value)
    if not 0 <= impact <= 10:
        raise argparse.ArgumentTypeError("impact must be between 0 and 10")
    return impact


def cmd_triage(args):
    if all(value is None for value in (args.impact, args.disposition, args.commit, args.note)):
        raise CliError("give at least one of --impact, --disposition, --commit or --note")
    conn = db.connect(paths.state_dir() / "state.sqlite3")
    try:
        with db.transaction(conn):  # all or nothing
            for finding_id in args.ids:
                try:
                    db.triage(conn, finding_id, now=int(time.time()), rated_impact=args.impact,
                              disposition=args.disposition, fixed_in=args.commit, note=args.note, by=args.by)
                except KeyError:
                    raise CliError(f"no finding {finding_id}") from None
    finally:
        conn.close()
    print(f"triaged {len(args.ids)} finding(s)")
    return 0


def _job_and_agent(job_id):
    cfg = load_config()
    state = paths.state_dir()
    conn = db.connect(state / "state.sqlite3")
    try:
        job = db.get_job(conn, job_id)
    finally:
        conn.close()
    if job is None:
        raise CliError(f"no job {job_id}")
    if not job["session_id"]:
        raise CliError(f"job {job_id} has no agent session (status {job['status']})")
    return cfg, state, job, make_agent(cfg.agents[job["agent"]], state)


def _ensure_worktree(cfg, job):
    """Recreate a pruned review checkout at the same path, so the agent session's project matches."""
    svc = build_services(cfg)
    job_paths = pipeline.job_paths(svc.state, job["id"])
    if Path(job_paths.worktree).is_dir():
        return job_paths
    with pipeline.repo_lock(svc.git.mirror_path(svc.state, job["repo"])):
        mirror = svc.git.ensure_mirror(svc.state, job["repo"])
        try:
            svc.git.add_worktree(mirror, job["head_sha"], job_paths.worktree)
        except GitError:
            svc.git.run(["-C", str(mirror), "fetch", "--quiet", "--no-tags", "origin",
                         f"+refs/pull/{job['number']}/head:refs/autoreview/pr/{job['number']}/head"])
            svc.git.add_worktree(mirror, job["head_sha"], job_paths.worktree)
    Path(job_paths.out).mkdir(parents=True, exist_ok=True)
    print(f"recreated checkout at {job_paths.worktree}", file=sys.stderr)
    return job_paths


def cmd_resume(args):
    cfg, _state, job, agent = _job_and_agent(args.job)
    job_paths = _ensure_worktree(cfg, job)
    argv, extra_env = agent.resume_interactive(paths=job_paths, session_id=job["session_id"])
    env = dict(os.environ)
    env.pop("GH_TOKEN", None)
    env.update(extra_env or {})
    os.chdir(job_paths.worktree)
    print(f"resuming job {job['id']} ({job['agent']} session {job['session_id']}) in {job_paths.worktree}",
          file=sys.stderr)
    os.execvpe(argv[0], argv, env)


def cmd_ask(args):
    cfg, _state, job, agent = _job_and_agent(args.job)
    job_paths = _ensure_worktree(cfg, job)
    returncode, answer = agent.ask(paths=job_paths, session_id=job["session_id"], question=args.question)
    print(answer.strip())
    return 0 if returncode == 0 else 1


def cmd_codex_login(args):
    cfg = load_config()
    agent = make_agent(cfg.agents["codex"], paths.state_dir())
    env = agent.ensure_home()
    print(f"Logging Codex in for reviews (CODEX_HOME={agent.home}); this is separate from ~/.codex.",
          file=sys.stderr)
    os.execvpe(cfg.agents["codex"].command, [cfg.agents["codex"].command, "login", "--device-auth"],
               {**os.environ, **env})


def cmd_config_check(args):
    cfg = load_config()
    print(f"config OK: {cfg.path}")
    for repo in cfg.repos.values():
        print(f"  {repo.name}: agents={','.join(repo.agents)} authors={','.join(repo.authors)} post={repo.post}")
    if not cfg.repos:
        print("  (no [[repo]] entries yet)")
    return 0


def cmd_housekeeping(args):
    maintenance.housekeeping(build_services(load_config()))
    return 0


def cmd_doctor(args):
    failures = 0

    def report(ok, message, warn=False):
        nonlocal failures
        mark = "ok  " if ok else ("warn" if warn else "FAIL")
        if not ok and not warn:
            failures += 1
        print(f"[{mark}] {message}")

    try:
        cfg = load_config()
        report(True, f"config {cfg.path}: {len(cfg.repos)} repo(s)")
    except ConfigError as exc:
        report(False, str(exc))
        return 1
    state = paths.state_dir()
    try:
        conn = db.connect(state / "state.sqlite3")
        conn.close()
        report(True, f"state {state} (schema {db.SCHEMA_VERSION})")
    except db.SchemaTooNew as exc:
        report(False, str(exc))
    try:
        token = read_token(paths.token_file())
        github = GitHub(token, state / "gh")
        report(True, f"GitHub token: authenticated as {github.viewer_login()}")
        expiry = github.token_expiry()
        if expiry:
            days = (expiry - datetime.now(timezone.utc)).days
            report(days > 30, f"GitHub token expires {expiry:%Y-%m-%d} ({days} days)", warn=days > 0)
        for repo in cfg.repos.values():
            try:
                github.list_open_prs(repo.name, limit=1)
                report(True, f"{repo.name}: pull requests readable")
            except GitHubError as exc:
                report(False, f"{repo.name}: {exc}")
    except GitHubError as exc:
        report(False, str(exc))
    used = {agent for repo in cfg.repos.values() for agent in repo.agents} or set(cfg.agents)
    for name in sorted(used):
        agent_cfg = cfg.agents[name]
        if not shutil.which(agent_cfg.command):
            report(False, f"{name}: command {agent_cfg.command!r} not found")
            continue
        ok, message = make_agent(agent_cfg, state).doctor()
        report(ok, message)
    try:
        proc = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True,
                              timeout=30)
        manager = proc.stdout.strip()
        report(manager in ("running", "degraded"), f"systemd user manager: {manager or proc.stderr.strip()}",
               warn=manager == "degraded")
        for unit in ("local-autoreviewer-worker.service", "local-autoreviewer-poll.timer"):
            enabled = subprocess.run(["systemctl", "--user", "is-enabled", unit], capture_output=True,
                                     text=True, timeout=30).stdout.strip() or "not installed"
            active = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True,
                                    text=True, timeout=30).stdout.strip()
            report(True, f"{unit}: {enabled}, {active}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        report(False, f"systemctl --user unavailable: {exc}")
    print(f"version {paths.read_version()}")
    return 1 if failures else 0


def build_parser():
    parser = argparse.ArgumentParser(prog="local-autoreviewer",
                                     description="Review your GitHub PRs with local Claude Code / Codex sessions.")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {paths.read_version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("poll", help="poll configured repos once and queue reviews")
    p.add_argument("--dry-run", action="store_true", help="show the events without recording anything")
    p.set_defaults(func=cmd_poll)

    sub.add_parser("worker", help="run the queue worker (systemd service)").set_defaults(func=cmd_worker)

    p = sub.add_parser("review", help="review one PR now, in the foreground")
    p.add_argument("ref", help="owner/repo#N or PR URL")
    p.add_argument("--agent", action="append", choices=config_mod.AGENTS)
    p.add_argument("--post", action="store_true", help="post the review (default: save it locally only)")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("enqueue", help="queue a review for the worker (any author)")
    p.add_argument("ref")
    p.add_argument("--agent", action="append", choices=config_mod.AGENTS)
    p.add_argument("--force", action="store_true", help="review again even if this head was reviewed")
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("status", help="show recent jobs")
    p.add_argument("--json", action="store_true")
    p.add_argument("-n", "--limit", type=int, default=15)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("resume", help="continue a review's agent session interactively")
    p.add_argument("job", type=int)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("ask", help="ask a review's agent session a follow-up question")
    p.add_argument("job", type=int)
    p.add_argument("question")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("findings", help="list recorded review findings (--json exports them for analysis)")
    p.add_argument("ref", nargs="?", help="owner/repo#N: only this PR")
    p.add_argument("--job", type=int, help="only this job")
    p.add_argument("--kind", action="append", choices=db.FINDING_KINDS)
    p.add_argument("--untriaged", action="store_true", help="only items with no disposition yet")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_findings)

    p = sub.add_parser("triage", help="record what was done about findings")
    p.add_argument("ids", type=int, nargs="+", metavar="ID", help="finding ids, as `findings` lists them")
    p.add_argument("--impact", type=_impact_arg,
                   help="your rating, 0-10: 0 = matters only for a cosmic-ray bit flip, 10 = the app would not work")
    p.add_argument("--disposition", choices=db.DISPOSITIONS)
    p.add_argument("--commit", help="the commit that fixed it")
    p.add_argument("--note")
    p.add_argument("--by", default=os.environ.get("USER", ""), help="who triaged (default: $USER)")
    p.set_defaults(func=cmd_triage)

    sub.add_parser("doctor", help="check config, credentials, agents and units").set_defaults(func=cmd_doctor)
    sub.add_parser("codex-login", help="log Codex in for reviews (separate CODEX_HOME)").set_defaults(
        func=cmd_codex_login)
    sub.add_parser("config-check", help="validate the config file").set_defaults(func=cmd_config_check)
    sub.add_parser("housekeeping", help="expire old job dirs and prune mirrors").set_defaults(
        func=cmd_housekeeping)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args) or 0
    except (CliError, ConfigError, GitHubError, GitError, db.SchemaTooNew) as exc:
        print(f"local-autoreviewer: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
