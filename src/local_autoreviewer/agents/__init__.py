# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent adapters: argv/env construction and result parsing for each CLI."""

from .claude import ClaudeAgent
from .codex import CodexAgent

ADAPTERS = {"claude": ClaudeAgent, "codex": CodexAgent}


def make_agent(cfg, state_dir):
    return ADAPTERS[cfg.name](cfg, state_dir)
