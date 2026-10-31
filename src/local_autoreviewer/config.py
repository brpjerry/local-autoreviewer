# SPDX-License-Identifier: GPL-3.0-or-later
"""TOML configuration with strict validation.

The config selects repos, agents and review policy only. Agent flags, the
process limits' existence and the token handling are fixed in code, so no
config value can widen what an agent may do.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .suggestions import DEFAULT_TEST_PATHS

AGENTS = ("claude", "codex")
TOKEN_OWNER = "@token-owner"
_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_MEMORY_RE = re.compile(r"^\d+[KMG]$")


class ConfigError(ValueError):
    pass


def parse_duration(value, key):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    match = _DURATION_RE.match(str(value).strip())
    if not match:
        raise ConfigError(f"{key}: expected a duration like '90s', '20m', '14d', got {value!r}")
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


@dataclass(frozen=True)
class AgentConfig:
    name: str
    command: str
    model: str = ""
    effort: str = ""
    memory_max: str = "3G"
    tasks_max: int = 512
    mode: str = "shell"  # codex only: "shell" | "inline"


@dataclass(frozen=True)
class RepoConfig:
    name: str
    agents: tuple
    authors: tuple
    project_docs: bool
    max_changed_files: int
    max_diff_bytes: int
    diff_exclude: tuple
    test_paths: tuple
    post: bool
    extra_instructions: str

    @property
    def owner(self):
        return self.name.split("/", 1)[0]


@dataclass(frozen=True)
class ServiceConfig:
    paused: bool = False
    review_timeout: int = 40 * 60
    settle: int = 3 * 60
    max_reviews_per_day: int = 30
    bootstrap: str = "baseline"
    retain_jobs: int = 14 * 86400
    max_attempts: int = 3


@dataclass(frozen=True)
class Config:
    service: ServiceConfig
    agents: dict
    repos: dict = field(default_factory=dict)
    path: Path = None

    def repo(self, name):
        return self.repos.get(name.lower())


_SERVICE_KEYS = {"paused", "review_timeout", "settle", "max_reviews_per_day", "bootstrap", "retain_jobs", "max_attempts"}
_REPO_KEYS = {"agents", "authors", "project_docs", "max_changed_files", "max_diff_bytes", "diff_exclude", "test_paths",
              "post", "extra_instructions"}
_AGENT_KEYS = {"command", "model", "effort", "memory_max", "tasks_max", "mode"}
_DEFAULTS = {
    "agents": ["claude", "codex"],
    "authors": [TOKEN_OWNER],
    "project_docs": True,
    "max_changed_files": 300,
    "max_diff_bytes": 1_500_000,
    "diff_exclude": [
        "**/*.lock", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock",
        "**/Cargo.lock", "**/go.sum", "**/dist/**", "**/*.min.js",
    ],
    "test_paths": list(DEFAULT_TEST_PATHS),
    "post": True,
    "extra_instructions": "",
}


def _check_keys(table, allowed, where):
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _typed(value, kind, key):
    if kind is int and isinstance(value, bool):
        raise ConfigError(f"{key}: expected an integer")
    if not isinstance(value, kind):
        raise ConfigError(f"{key}: expected {kind.__name__}, got {type(value).__name__}")
    return value


def _str_list(value, key):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{key}: expected a list of strings")
    return tuple(value)


def _repo_settings(table, base, where):
    _check_keys(table, _REPO_KEYS, where)
    merged = dict(base)
    merged.update(table)
    agents = _str_list(merged["agents"], f"{where}.agents")
    for agent in agents:
        if agent not in AGENTS:
            raise ConfigError(f"{where}.agents: unknown agent {agent!r}")
    if len(set(agents)) != len(agents):
        raise ConfigError(f"{where}.agents: duplicate agent")
    authors = tuple(a.lower() for a in _str_list(merged["authors"], f"{where}.authors"))
    return {
        "agents": agents,
        "authors": authors,
        "project_docs": _typed(merged["project_docs"], bool, f"{where}.project_docs"),
        "max_changed_files": _typed(merged["max_changed_files"], int, f"{where}.max_changed_files"),
        "max_diff_bytes": _typed(merged["max_diff_bytes"], int, f"{where}.max_diff_bytes"),
        "diff_exclude": _str_list(merged["diff_exclude"], f"{where}.diff_exclude"),
        "test_paths": _str_list(merged["test_paths"], f"{where}.test_paths"),
        "post": _typed(merged["post"], bool, f"{where}.post"),
        "extra_instructions": _typed(merged["extra_instructions"], str, f"{where}.extra_instructions"),
    }


def _agent(name, table):
    where = f"agents.{name}"
    _check_keys(table, _AGENT_KEYS, where)
    values = {"command": name}
    values.update(table)
    for key in ("command", "model", "effort", "memory_max", "mode"):
        if key in values:
            _typed(values[key], str, f"{where}.{key}")
    if "tasks_max" in values:
        _typed(values["tasks_max"], int, f"{where}.tasks_max")
    if "memory_max" in values and not _MEMORY_RE.match(values["memory_max"]):
        raise ConfigError(f"{where}.memory_max: expected e.g. '3G' or '512M'")
    if values.get("mode", "shell") not in ("shell", "inline"):
        raise ConfigError(f"{where}.mode: expected 'shell' or 'inline'")
    if name != "codex" and values.get("mode", "shell") != "shell":
        raise ConfigError(f"{where}.mode: only codex supports 'inline'")
    return AgentConfig(name=name, **values)


def parse(data, path=None):
    _check_keys(data, {"service", "defaults", "agents", "repo"}, "config")
    service_table = data.get("service", {})
    _check_keys(service_table, _SERVICE_KEYS, "service")
    service = ServiceConfig(
        paused=_typed(service_table.get("paused", False), bool, "service.paused"),
        review_timeout=parse_duration(service_table.get("review_timeout", "40m"), "service.review_timeout"),
        settle=parse_duration(service_table.get("settle", "3m"), "service.settle"),
        max_reviews_per_day=_typed(service_table.get("max_reviews_per_day", 30), int, "service.max_reviews_per_day"),
        bootstrap=service_table.get("bootstrap", "baseline"),
        retain_jobs=parse_duration(service_table.get("retain_jobs", "14d"), "service.retain_jobs"),
        max_attempts=_typed(service_table.get("max_attempts", 3), int, "service.max_attempts"),
    )
    if service.bootstrap not in ("baseline", "review"):
        raise ConfigError("service.bootstrap: expected 'baseline' or 'review'")

    agents_table = data.get("agents", {})
    _check_keys(agents_table, set(AGENTS), "agents")
    agents = {name: _agent(name, agents_table.get(name, {})) for name in AGENTS}

    # Validate [defaults] on its own so errors there are reported even with no repos.
    _repo_settings(data.get("defaults", {}), _DEFAULTS, "defaults")
    base = {**_DEFAULTS, **data.get("defaults", {})}
    repos = {}
    repo_tables = data.get("repo", [])
    if not isinstance(repo_tables, list):
        raise ConfigError("repo: use [[repo]] tables")
    for index, table in enumerate(repo_tables):
        where = f"repo[{index}]"
        if "name" not in table:
            raise ConfigError(f"{where}: missing name")
        name = table["name"]
        if not isinstance(name, str) or not _REPO_RE.match(name):
            raise ConfigError(f"{where}.name: expected 'owner/repo', got {name!r}")
        if name.lower() in repos:
            raise ConfigError(f"{where}.name: duplicate repo {name}")
        settings = _repo_settings({k: v for k, v in table.items() if k != "name"}, base, where)
        repos[name.lower()] = RepoConfig(name=name, **settings)
    return Config(service=service, agents=agents, repos=repos, path=path)


def load(path):
    path = Path(path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path} (copy config.example.toml there)") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return parse(data, path)


class ConfigWatcher:
    """Reload the config when its mtime changes; keep the last good one on errors."""

    def __init__(self, path, log):
        self.path = Path(path)
        self.log = log
        self.mtime = None
        self.current = None

    def get(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if self.current is None or mtime != self.mtime:
            try:
                self.current = load(self.path)
                if self.mtime is not None:
                    self.log.info("config reloaded from %s", self.path)
            except ConfigError as exc:
                if self.current is None:
                    raise
                self.log.error("config reload failed, keeping previous config: %s", exc)
            self.mtime = mtime
        return self.current
