# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent adapters: argv/env construction and result parsing for each CLI."""

from .claude import ClaudeAgent
from .codex import CodexAgent

ADAPTERS = {"claude": ClaudeAgent, "codex": CodexAgent}


def make_agent(cfg, state_dir):
    """The adapter for an agent's type: every reviewer of a type gets that type's locked flags."""
    return ADAPTERS[cfg.type](cfg, state_dir)
