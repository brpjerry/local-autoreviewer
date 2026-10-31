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
ANCHOR_WINDOW = 30  # lines around a stated range searched for the nearest copy of `current`; beyond, only a unique copy
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
    """The edit, placed in the file: with the lines it replaces (`original`) and how it was placed
    (`anchor`), or None if it can't apply (a path outside the checkout, a missing file, or, for an edit
    without `current`, a range past the end of the file).

    A reviewer gives the text it means to replace (`current`), and its line numbers are sometimes off.
    So the edit goes where that text is (see _locate): "matched" at the stated lines, "moved" elsewhere.
    If the text can't be placed, the edit is kept for the record but not applied: "dropped". Moved and
    dropped edits keep the reviewer's numbers in `stated`. An edit without `current` (from an older
    review) is placed by its line numbers: "unverified"."""
    root = Path(worktree).resolve()
    target = (root / edit["file"]).resolve()
    if root not in target.parents or not target.is_file():
        return None
    text = target.read_text(errors="replace")
    lines = text.removesuffix("\n").split("\n") if text else []  # at "\n" only, as `current` and GitHub count
    start, end = edit["start_line"], edit["end_line"]
    if edit.get("current") is None:
        if not 1 <= start <= end <= len(lines):
            return None
        return {**edit, "original": lines[start - 1:end], "anchor": "unverified"}
    wanted = edit["current"].split("\n")  # "" is one blank line
    if len(wanted) > end - start + 1 and wanted[-1] == "":  # a final newline, not a blank last line
        wanted.pop()
    found = _locate(lines, [line.rstrip() for line in wanted], start)
    if found is None:
        return {**edit, "original": [], "anchor": "dropped", "stated": [start, end]}
    first, last = found + 1, found + len(wanted)
    if (first, last) == (start, end):
        return {**edit, "original": lines[found:last], "anchor": "matched"}
    return {**edit, "start_line": first, "end_line": last, "original": lines[found:last], "anchor": "moved",
            "stated": [start, end]}


def _locate(lines, wanted, start):
    """Where the lines `wanted` are in the file (0-based), ignoring trailing whitespace (often lost in
    copying), or None. The copy nearest the stated start, at most ANCHOR_WINDOW lines away, wins; failing
    that, the only copy in the file. Two copies equally near, or blank lines away from the stated ones,
    say nothing about where the edit belongs."""
    stripped = [line.rstrip() for line in lines]

    def at(i):
        return stripped[i:i + len(wanted)] == wanted
    if not any(wanted):
        return start - 1 if at(start - 1) else None
    window = range(max(0, start - 1 - ANCHOR_WINDOW), min(len(lines) - len(wanted), start - 1 + ANCHOR_WINDOW) + 1)
    near = sorted((i for i in window if at(i)), key=lambda i: abs(i + 1 - start))
    if near:
        tied = len(near) > 1 and abs(near[0] + 1 - start) == abs(near[1] + 1 - start)
        return None if tied else near[0]
    anywhere = [i for i in range(len(lines) - len(wanted) + 1) if at(i)]
    return anywhere[0] if len(anywhere) == 1 else None


def applied(edits):
    """The edits that go into the review: all but the dropped ones."""
    return [e for e in edits if e.get("anchor") != "dropped"]


def loc_delta(edits, test_paths):
    """Net change in the number of non-test lines if the edits are applied."""
    return sum(len(replacement_lines(e["replacement"])) - len(e["original"])
               for e in applied(edits) if not is_test_path(e["file"], test_paths))


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
        # Readability rates the proposed fix; if none of it can be applied, the review doesn't show it.
        readability = item["readability"] if applied(edits) or not item["edits"] else "same"
        final, notes = adjust(item["severity"], delta, readability)
        findings.append({**item, "edits": [_mark_inline(e, hunks) for e in edits], "loc_delta": delta,
                         "reviewer_severity": item["severity"], "severity": final, "severity_notes": notes})
    findings.sort(key=lambda f: LEVELS.index(f["severity"]), reverse=True)
    nits = [{**nit, "edits": [_mark_inline(e, hunks) for e in (check_edit(e, worktree) for e in nit["edits"]) if e]}
            for nit in review["nits"]]
    return {**review, "findings": findings, "nits": nits}


def _mark_inline(edit, hunks):
    if edit.get("anchor") == "dropped":
        return {**edit, "inline": False}
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
