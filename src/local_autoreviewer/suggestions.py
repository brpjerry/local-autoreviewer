# SPDX-License-Identifier: GPL-3.0-or-later
"""Reviewers' proposed changes: check them against the checkout, measure what they do to the size of
the code, adjust each finding's severity, and turn the edits GitHub can show into suggested changes.

Severity modifiers (blockers are exempt): a fix that removes non-test lines goes up one level; a fix
the reviewer rates less readable goes down two. The levels are nit < minor < major < blocker, so a
finding can end up a nit, and one only goes up to blocker from major.
"""

import re
from pathlib import Path

LEVELS = ("nit", "minor", "major", "blocker")
DEFAULT_TEST_PATHS = (
    "tests/**", "test/**", "**/tests/**", "**/test/**", "spec/**", "**/spec/**",
    "**/test_*", "**/*_test.*", "**/*.test.*", "**/*.spec.*",
)
MAX_EDITS = 10  # per finding
MAX_REPLACEMENT = 20_000  # characters
MAX_SHOWN_LINES = 80  # per edit, in the review body; the inline suggestion is complete
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def diff_hunks(diff):
    """Right-hand (head) line ranges of each hunk in a unified diff, per file: {path: [(first, last)]}.
    GitHub only accepts review comments on lines inside these ranges."""
    hunks, path = {}, None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = target[2:] if target.startswith("b/") else None  # /dev/null, or a quoted odd path
            continue
        match = _HUNK_RE.match(line)
        if match and path is not None:
            first, count = int(match.group(1)), int(match.group(2) or 1)
            if count:
                hunks.setdefault(path, []).append((first, first + count - 1))
    return hunks


def _glob_re(pattern):
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def is_test_path(path, test_paths):
    return any(_glob_re(pattern).match(path) for pattern in test_paths)


def replacement_lines(text):
    """The lines an edit puts in place of its range; an empty replacement deletes the range."""
    text = text.removesuffix("\n")
    return text.split("\n") if text else []


def check_edit(edit, worktree):
    """The edit with the lines it replaces added (`original`), or None if it can't apply: a path
    outside the checkout, a missing file, or a line range past the end of the file."""
    root = Path(worktree).resolve()
    target = (root / edit["file"]).resolve()
    if root not in target.parents or not target.is_file():
        return None
    lines = target.read_text(errors="replace").splitlines()
    if not 1 <= edit["start_line"] <= edit["end_line"] <= len(lines):
        return None
    return {**edit, "original": lines[edit["start_line"] - 1:edit["end_line"]]}


def loc_delta(edits, test_paths):
    """Net change in the number of non-test lines if the edits are applied."""
    return sum(len(replacement_lines(e["replacement"])) - len(e["original"])
               for e in edits if not is_test_path(e["file"], test_paths))


def adjust(severity, delta, readability):
    """Final severity and the reasons it differs from the reviewer's."""
    if severity == "blocker":
        return severity, []
    level, notes = LEVELS.index(severity), []
    if delta < 0:
        level += 1
        notes.append(f"removes {-delta} line{'s' if delta < -1 else ''}")
    if readability == "worse":
        level -= 2
        notes.append("less readable")
    return LEVELS[max(0, min(level, len(LEVELS) - 1))], notes


def apply(review, *, worktree, hunks, test_paths):
    """Check every finding's and nit's edits, adjust finding severities, and mark which edits GitHub can
    show inline. Findings are re-sorted by final severity, so positions match the posted numbering."""
    findings = []
    for item in review["findings"]:
        edits = [e for e in (check_edit(e, worktree) for e in item["edits"]) if e]
        delta = loc_delta(edits, test_paths)
        final, notes = adjust(item["severity"], delta, item["readability"])
        findings.append({**item, "edits": [_mark_inline(e, hunks) for e in edits], "loc_delta": delta,
                         "reviewer_severity": item["severity"], "severity": final, "severity_notes": notes})
    findings.sort(key=lambda f: LEVELS.index(f["severity"]), reverse=True)
    nits = [{**nit, "edits": [_mark_inline(e, hunks) for e in (check_edit(e, worktree) for e in nit["edits"]) if e]}
            for nit in review["nits"]]
    return {**review, "findings": findings, "nits": nits}


def _mark_inline(edit, hunks):
    inline = any(first <= edit["start_line"] and edit["end_line"] <= last for first, last in hunks.get(edit["file"], ()))
    return {**edit, "inline": inline}


def fence(info, text):
    """A fenced block that text can't close early, whatever backticks it contains."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    body = "\n".join(replacement_lines(text))
    return f"{ticks}{info}\n{body + chr(10) if body else ''}{ticks}"


def comments(review, label):
    """GitHub review comments with a suggested change, one per inline edit. label(index, item) gives
    the heading that ties the comment to its item in the review body."""
    out = []
    items = [(i, f) for i, f in enumerate(review["findings"])] + [(None, n) for n in review["nits"]]
    for index, item in items:
        for edit in item.get("edits") or []:
            if not edit.get("inline"):
                continue
            comment = {"path": edit["file"], "line": edit["end_line"], "side": "RIGHT",
                       "body": f"{label(index, item)}\n\n{fence('suggestion', edit['replacement'])}"}
            if edit["start_line"] < edit["end_line"]:
                comment.update(start_line=edit["start_line"], start_side="RIGHT")
            out.append(comment)
    return out


def shown_diff(edit):
    """The edit as a diff for the review body, shortened if long."""
    lines = [f"-{line}" for line in edit["original"]] + [f"+{line}" for line in replacement_lines(edit["replacement"])]
    if len(lines) > MAX_SHOWN_LINES:
        lines = lines[:MAX_SHOWN_LINES] + [f"… {len(lines) - MAX_SHOWN_LINES} more lines"]
    return fence("diff", "\n".join(lines))
