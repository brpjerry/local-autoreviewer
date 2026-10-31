# SPDX-License-Identifier: GPL-3.0-or-later
"""The duplicate check that lets several reviews of one commit be posted as one.

Before the reviews are posted together, one of the reviewing agents gets every item of every review and
names the sets that report the same problem. Each set keeps one item, credited to all its reviewers; the
rest are recorded as duplicates of it (findings.duplicate_of) and left out of the post.
"""

import json
from string import Template

from . import paths, review, suggestions

PROMPT_FILES = ("duplicates.md", "duplicates.schema.json")
LETTERS = {"findings": "F", "nits": "N", "test_gaps": "T", "questions": "Q"}
MAX_DETAIL = 1_500  # characters of a finding's detail the check sees


def snapshot():
    return {name: (paths.prompts_dir() / name).read_text() for name in PROMPT_FILES}


def catalogue(members):
    """Every item of the reviews, by the id the check knows it by ("F1", "N2", ...):
    {id: (job_id, key, position)}. members are in display order, as review.merge() takes them."""
    ids, counts = {}, dict.fromkeys(LETTERS.values(), 0)
    for member in members:
        for key, letter in LETTERS.items():
            for position, _ in enumerate(member["review"].get(key) or []):
                counts[letter] += 1
                ids[f"{letter}{counts[letter]}"] = (member["job_id"], key, position)
    return ids


def worth_checking(members):
    """Duplicates need items from at least two reviewers."""
    return sum(any(member["review"].get(key) for key in LETTERS) for member in members) > 1


def _shown(member, key, item):
    reviewer = review.AGENT_NAMES.get(member["agent"], member["agent"])
    if key in ("test_gaps", "questions"):
        return {"reviewer": reviewer, "kind": key[:-1], "text": item}
    if key == "nits":
        return {"reviewer": reviewer, "kind": "nit", "file": item["file"], "line": item["line"], "text": item["text"]}
    return {"reviewer": reviewer, "kind": "finding", "severity": item["severity"], "file": item["file"],
            "line": item["line"], "title": item["title"], "trigger": item.get("trigger") or "",
            "detail": item["detail"][:MAX_DETAIL], "suggestion": item["suggestion"][:MAX_DETAIL]}


def build_prompt(members, *, prompts, repo, number, title, head_sha, worktree, agent_note):
    by_job = {member["job_id"]: member for member in members}
    items = []
    for item_id, (job_id, key, position) in catalogue(members).items():
        member = by_job[job_id]
        items.append({"id": item_id, **_shown(member, key, member["review"][key][position])})
    reviewers = review.reviewer_names([member["agent"] for member in members])
    return Template(prompts["duplicates.md"]).safe_substitute(
        reviewers=reviewers, repo=repo, number=number, title=title.replace("\n", " "), head_sha=head_sha,
        worktree=worktree, agent_note=agent_note,
        items=suggestions.fence("json", json.dumps(items, indent=1, ensure_ascii=False)))


def groups(raw, members):
    """The sets of duplicates in the check's answer that can be merged, as lists of (job_id, key, position).
    A set must span two reviewers; a finding may repeat a nit, but a test gap only repeats a test gap and a
    question only a question. An item belongs to the first set that names it; unknown ids are ignored."""
    ids = catalogue(members)
    seen, out = set(), []
    answer = raw.get("duplicates") if isinstance(raw, dict) else None
    for entry in answer if isinstance(answer, list) else []:
        named = entry.get("ids") if isinstance(entry, dict) else None
        named = [i for i in named if isinstance(i, str)] if isinstance(named, list) else []
        refs = [ids[i] for i in dict.fromkeys(named) if i in ids and ids[i] not in seen]
        kinds = {key for _, key, _ in refs}
        if len({job_id for job_id, _, _ in refs}) < 2 or (len(kinds) > 1 and not kinds <= {"findings", "nits"}):
            continue
        out.append(refs)
        seen.update(refs)
    return out


def duplicates(members, sets):
    """Which item of each set is kept, as review.merge() takes it: {repeat: kept}. A finding wins over a
    nit, then the higher final severity, then one with a proposed change that applies, then the higher
    impact, then the reviewer listed first."""
    by_job = {member["job_id"]: member for member in members}
    order = {member["job_id"]: n for n, member in enumerate(members)}

    def rank(ref):
        job_id, key, position = ref
        item = by_job[job_id]["review"][key][position]
        if key != "findings":
            return (0, 0, bool(suggestions.applied(item.get("edits") or [])) if key == "nits" else False, -1,
                    -order[job_id], -position)
        return (1, suggestions.LEVELS.index(item["severity"]), bool(suggestions.applied(item.get("edits") or [])),
                item.get("impact") if item.get("impact") is not None else -1, -order[job_id], -position)
    mapping = {}
    for refs in sets:
        kept = max(refs, key=rank)
        mapping.update({ref: kept for ref in refs if ref != kept})
    return mapping
