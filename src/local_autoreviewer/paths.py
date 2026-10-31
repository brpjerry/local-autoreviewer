# SPDX-License-Identifier: GPL-3.0-or-later
"""Filesystem locations for config, state and packaged data."""

import os
from pathlib import Path

APP = "local-autoreviewer"
PACKAGE_DIR = Path(__file__).resolve().parent
INSTALLED_SHARE = Path("/usr/share") / APP


def _xdg(name, fallback):
    value = os.environ.get(name)
    return Path(value) if value else Path.home() / fallback


def config_dir():
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def config_file():
    return Path(os.environ.get("LOCAL_AUTOREVIEWER_CONFIG", config_dir() / "config.toml"))


def token_file():
    return config_dir() / "github-token"


def state_dir():
    # systemd sets STATE_DIRECTORY for StateDirectory=; the CLI falls back to XDG.
    value = os.environ.get("STATE_DIRECTORY")
    if value:
        return Path(value.split(":")[0])
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def share_dir():
    """Prompts and VERSION: the checkout when run from source, else /usr/share."""
    override = os.environ.get("LOCAL_AUTOREVIEWER_SHARE")
    if override:
        return Path(override)
    checkout = PACKAGE_DIR.parent.parent
    if (checkout / "prompts" / "review.md").is_file():
        return checkout
    return INSTALLED_SHARE


def prompts_dir():
    return share_dir() / "prompts"


def version_file():
    return share_dir() / "VERSION"


def read_version():
    try:
        return version_file().read_text().strip()
    except OSError:
        return "dev"
