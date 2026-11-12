# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan usage limits, checked before a review starts, so reviews leave the operator a reserve.

Each agent type reads its own login's limits (the adapters' read_limits). A reading is kept for FRESH seconds
per login: the usage endpoints are rate limited, and the operator's own tools poll them too. A reading that
fails counts as unknown, and the review runs. A review whose agent then finds a limit used up is skipped as
well, and remembered for FRESH seconds for its login and model (see note_used_up).
"""

import logging
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime

FRESH = 600
log = logging.getLogger(__name__)


class Unavailable(Exception):
    """The limits can't be read now; the message says why (never a credential)."""


@dataclass(frozen=True)
class Limit:
    name: str  # "5-hour", "weekly", "weekly (Fable)"
    used: float  # percent of the limit used
    resets_at: int = 0  # epoch seconds; 0 if not known
    model: str = ""  # set for a limit on some models only (part of their name, such as a provider prefix)

    def left(self, now):
        """Percent left: all of it once the window has reset."""
        if self.resets_at and self.resets_at <= now:
            return 100.0
        return max(0.0, 100.0 - self.used)

    def applies_to(self, model):
        return not self.model or (bool(model) and self.model.lower() in model.lower())


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def epoch(value):
    """An ISO 8601 time or epoch seconds as epoch seconds; 0 if neither."""
    if number(value):
        return int(value) if value > 0 else 0
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except (TypeError, ValueError):
        return 0


_readings, _locks, _guard = {}, {}, threading.Lock()


def read(agent, now):
    """The agent's login's limits, or Unavailable: read at most once per FRESH seconds per login. Reviews that
    start together wait for one reading instead of each asking."""
    key = agent.limits_key()
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        cached = _readings.get(key)
        if cached is None or now - cached[0] >= FRESH:
            try:
                value = agent.read_limits()
            except Unavailable as exc:
                value = exc
                log.info("%s: can't read the plan's usage limits (%s); reviews run without the check", key, exc)
            except Exception as exc:  # the check is optional: an unexpected failure mustn't fail the review
                value = Unavailable(f"unexpected {type(exc).__name__}")
                log.warning("%s: reading the plan's usage limits failed (%s); reviews run without the check", key,
                            type(exc).__name__, exc_info=True)
            cached = _readings[key] = (now, value)
        return cached[1]


# An agent's error that says the plan is used up, as opposed to a brief throttle ("rate limit exceeded", HTTP 429)
# that a later retry gets past.
_USED_UP_RE = re.compile(r"usage limit|limit reached|quota|out of (?:credits|usage)|free.?limit|credit balance",
                         re.IGNORECASE)
_hits = {}  # (limits_key, model) -> when a review on that login and model found a limit used up


def used_up(message):
    return bool(_USED_UP_RE.search(message or ""))


def note_used_up(agent, model, now):
    """A review found a usage limit used up. For FRESH seconds, the reviews of that login and model are skipped
    without starting; those on its other models, whose quotas may be their own, aren't, and the login's measured
    reading stands. After that, a free tier, which reports no limits, is simply tried again."""
    with _guard:
        _hits[(agent.limits_key(), model)] = now
    log.info("%s: a review on %s found a usage limit used up; its next reviews are skipped for %d minutes",
             agent.limits_key(), model or "the default model", FRESH // 60)


def hit_recently(agent, model, now):
    with _guard:
        at = _hits.get((agent.limits_key(), model))
    return at is not None and now - at < FRESH


def describe(limit, now):
    when = (f", resets {datetime.fromtimestamp(limit.resets_at).astimezone():%a %H:%M}"
            if limit.resets_at > now else "")
    return f"{limit.name} limit {limit.left(now):.0f}% left{when}"


def low(limits, model, reserve, now):
    """The first limit with less than reserve percent left that applies to a reviewer running model, or None.
    limits: a list of Limit, or Unavailable (unknown, so none is low)."""
    if reserve <= 0 or isinstance(limits, Unavailable):
        return None
    return next((limit for limit in limits if limit.applies_to(model) and limit.left(now) < reserve), None)
