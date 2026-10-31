#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Checks an installed local-autoreviewer package. Usage: check.sh PACKAGE_FILE (run as root).
set -euo pipefail
package=$1
pacman -Qk local-autoreviewer
namcap_output=$(namcap "$package" || true)
printf '%s\n' "$namcap_output"
if grep -q ' E: ' <<<"$namcap_output"; then
    echo 'namcap reported errors' >&2
    exit 1
fi
expected=(
    /usr/bin/local-autoreviewer
    /usr/lib/local-autoreviewer/local_autoreviewer/__main__.py
    /usr/share/local-autoreviewer/prompts/review.md
    /usr/share/local-autoreviewer/prompts/review.schema.json
    /usr/share/local-autoreviewer/prompts/followup.md
    /usr/share/local-autoreviewer/VERSION
    /usr/lib/systemd/user/autoreviewer.slice
    /usr/lib/systemd/user/local-autoreviewer-worker.service
    /usr/lib/systemd/user/local-autoreviewer-poll.service
    /usr/lib/systemd/user/local-autoreviewer-poll.timer
    /usr/share/doc/local-autoreviewer/config.example.toml
    /usr/share/doc/local-autoreviewer/README.md
)
# Check the archive rather than the disk: container images often NoExtract usr/share/doc.
listing=$(bsdtar -tf "$package")
for path in "${expected[@]}"; do
    grep -qxF "${path#/}" <<<"$listing" || { echo "package lacks $path" >&2; exit 1; }
done
example=$(mktemp --suffix=.toml)
bsdtar -xOf "$package" usr/share/doc/local-autoreviewer/config.example.toml > "$example"
# Run outside any checkout so a missing packaged file cannot be masked by the source tree.
cd /tmp
python -I - <<'PY'
import ast
import sys
from pathlib import Path
root = Path('/usr/lib/local-autoreviewer')
for path in root.rglob('*.py'):
    ast.parse(path.read_text(), filename=str(path))
sys.path.insert(0, str(root))
from local_autoreviewer import __main__, paths, pipeline, worker  # noqa: F401
assert paths.share_dir() == Path('/usr/share/local-autoreviewer'), paths.share_dir()
assert (paths.prompts_dir() / 'review.md').is_file()
assert paths.read_version() != 'dev'
PY
local-autoreviewer --help >/dev/null
LOCAL_AUTOREVIEWER_CONFIG=$example local-autoreviewer config-check
install -d -m700 /run/user/0
XDG_RUNTIME_DIR=/run/user/0 systemd-analyze --user verify \
    /usr/lib/systemd/user/local-autoreviewer-worker.service \
    /usr/lib/systemd/user/local-autoreviewer-poll.service \
    /usr/lib/systemd/user/local-autoreviewer-poll.timer
# Installing creates no configuration, state or enabled units.
test ! -e "$HOME/.config/local-autoreviewer"
test ! -e "$HOME/.local/state/local-autoreviewer"
test ! -e /etc/systemd/user/default.target.wants/local-autoreviewer-worker.service
echo "package checks passed: $(cat /usr/share/local-autoreviewer/VERSION)"
