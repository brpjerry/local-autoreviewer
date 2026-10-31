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

REVIEW = {
    "summary": "Adds a divide helper; one real bug.",
    "findings": [{"severity": "major", "category": "correctness", "file": "src/calc.py", "line": 2,
                  "title": "Division by zero is unhandled", "detail": "divide(1, 0) raises ZeroDivisionError.",
                  "suggestion": "Check b == 0 and raise ValueError."}],
    "test_gaps": ["No test for b == 0."],
    "questions": [],
    "nits": [{"file": "src/calc.py", "line": 1, "text": "Add a docstring."}],
}


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
    if mode == "sleep":
        time.sleep(60)
    if mode == "fail":
        print("boom: agent crashed", file=sys.stderr)
        return 3
    if mode == "leak":
        review["summary"] = "Found a token: ghp_" + "A" * 36
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
