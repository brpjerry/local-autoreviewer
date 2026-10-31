#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stand-in for `claude` and `codex` in tests.

Invoked through a wrapper script: fake_agent.py --mode MODE --log FILE -- <real agent argv...>
It records its argv, environment keys and cwd, then behaves according to MODE.
"""

import json
import os
import sys
import time
from pathlib import Path

REVIEW = {
    "summary": "Adds a divide helper; one real bug.",
    "findings": [{"severity": "major", "impact": 6, "likelihood": "common", "trigger": "Any caller passes b=0.",
                  "category": "correctness", "file": "src/calc.py", "line": 2,
                  "title": "Division by zero is unhandled", "detail": "divide(1, 0) raises ZeroDivisionError.",
                  "suggestion": "Check b == 0 and raise ValueError.", "readability": "same",
                  "edits": [{"file": "src/calc.py", "start_line": 2, "end_line": 2,
                             "replacement": "    if b == 0:\n        raise ValueError(\"b must not be zero\")\n"
                                            "    return a / b"}]}],
    "test_gaps": ["No test for b == 0."],
    "questions": [],
    "nits": [{"file": "src/calc.py", "line": 1, "text": "Add a docstring.",
              "edits": [{"file": "src/calc.py", "start_line": 1, "end_line": 1,
                         "replacement": "def divide(a, b):\n    \"\"\"Divide a by b.\"\"\""}]}],
}


def followup(context):
    """A follow-up answer: the first previous finding fixed, the rest not, and one regression."""
    previous = json.loads((context / "previous-findings.json").read_text())
    regression = dict(REVIEW["findings"][0], title="The fix dropped the docstring", severity="minor", impact=2,
                      likelihood="common", edits=[{"file": "src/calc.py", "start_line": 1, "end_line": 1,
                                                    "replacement": "def divide(a, b):\n    \"\"\"Divide.\"\"\""}])
    return {"summary": f"{len(previous)} previous finding(s) checked.",
            "resolutions": [{"finding": item["id"], "status": "fixed" if n == 0 else "not_fixed",
                             "detail": "checked"} for n, item in enumerate(previous)],
            "regressions": [regression]}


def anchored(review, offset):
    """The review with each edit's `current` copied from the checkout (the working directory), as a careful
    reviewer would; offset then shifts every stated range, as a reviewer that misremembers line numbers does."""
    def edit(e):
        path = Path(e["file"])
        lines = path.read_text().splitlines() if path.is_file() else []
        return {**e, "current": "\n".join(lines[e["start_line"] - 1:e["end_line"]]),
                "start_line": e["start_line"] + offset, "end_line": e["end_line"] + offset}
    out = dict(review)
    for key in ("findings", "nits", "regressions"):
        if key in out:
            out[key] = [{**item, "edits": [edit(e) for e in item.get("edits", [])]} for item in out[key]]
    return out


def duplicates(prompt):
    """A duplicate check's answer: items of the same kind with the same title or text are duplicates."""
    items = json.loads(prompt.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    sets = {}
    for item in items:
        sets.setdefault((item["kind"], item.get("title") or item["text"]), []).append(item["id"])
    return {"duplicates": [{"ids": ids, "reason": "same text"} for ids in sets.values() if len(ids) > 1]}


def value_after(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def main():
    args = sys.argv[1:]
    sep = args.index("--")
    opts, argv = args[:sep], args[sep + 1:]
    mode, log = value_after(opts, "--mode"), value_after(opts, "--log")
    prompt = sys.stdin.read()
    with open(log, "a") as handle:
        handle.write(json.dumps({"argv": argv, "env": sorted(os.environ), "cwd": os.getcwd(),
                                 "prompt": prompt, "pid": os.getpid()}) + "\n")
    codex = "exec" in argv
    review = dict(REVIEW)
    if "This is a follow-up, not a new review." in prompt:
        review = followup(Path(os.getcwd()).parent / "context")
    if mode == "trivial":
        review = dict(review, summary="Bumps the version.", trivial=True, findings=[], test_gaps=[], nits=[])
    if "Your only job is to find the duplicates" in prompt:
        if mode == "dupfail":
            print("boom: duplicate check crashed", file=sys.stderr)
            return 3
        review = duplicates(prompt)
        mode = {"dupsleep": "sleep", "dupratelimit": "ratelimit"}.get(mode, "ok")
    if mode == "sleep":
        time.sleep(60)
    if mode == "fail":
        print("boom: agent crashed", file=sys.stderr)
        return 3
    if mode == "leak":
        review["summary"] = "Found a token: ghp_" + "A" * 36
    if mode == "leak_edit":  # only in a proposed change, which is posted as an inline suggestion
        finding = dict(review["findings"][0])
        finding["edits"] = [dict(finding["edits"][0], replacement="    TOKEN = 'ghp_" + "B" * 36 + "'")]
        review["findings"] = [finding]
    review = anchored(review, 1 if mode == "offby1" else 0)
    if mode == "badjson":
        review = {"nope": True}
    if codex:
        print(json.dumps({"type": "thread.started", "thread_id": "thread-123"}))
        if mode == "ratelimit":
            print(json.dumps({"type": "error", "message": "You've hit your usage limit. Try again later."}))
            return 1
        with open(value_after(argv, "-o"), "w") as handle:
            json.dump(review, handle)
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}))
        return 0
    session = value_after(argv, "--session-id") or value_after(argv, "--resume") or "none"
    if mode == "ratelimit":
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                          "result": "Claude usage limit reached", "session_id": session}))
        return 1
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": session,
                      "structured_output": review, "result": json.dumps(review), "num_turns": 3,
                      "usage": {"input_tokens": 10}, "total_cost_usd": 0.01}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
