# Arch package

The `local-autoreviewer` package installs:
- the `local-autoreviewer` command;
- its prompts;
- four systemd **user** units: `autoreviewer.slice`, `local-autoreviewer-worker.service`, `local-autoreviewer-poll.service` and `local-autoreviewer-poll.timer`.

Installing enables nothing and creates no configuration. There is no install script. The code goes into a private directory, `/usr/lib/local-autoreviewer/`, not site-packages, so Python minor-version upgrades don't require a rebuild.

## How it is built

`prepare.py` stages a build from the checked-out source with no network access:
- a reproducible `local-autoreviewer.tar.gz` of the tracked files plus `.source-revision`;
- a `PKGBUILD` generated from `PKGBUILD.in`, with the version from `pyproject.toml` and the archive's SHA-256.

With `--tag vX.Y.Z`, it refuses a tag that doesn't match the version.

`build.sh` runs the whole flow inside a fresh `archlinux:base-devel` container. It:
1. stages the build;
2. runs `makepkg` as an unprivileged user, whose `check()` runs the offline test suite;
3. installs the result;
4. runs `check.sh`: `pacman -Qk`, namcap errors, the expected files, an import smoke test from outside the source tree, a validation of `config.example.toml`, `systemd-analyze --user verify`, and a check that nothing was enabled or configured;
5. writes the package and `SHA256SUMS` to the output directory.

CI (`.github/workflows/pacman.yml`) runs exactly this. To run it locally:

```sh
podman run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" docker.io/library/archlinux:base-devel \
    bash /src/packaging/arch/build.sh /out
```

For a quick build on an Arch machine without containers:

```sh
python packaging/arch/prepare.py /tmp/local-autoreviewer-pkg
cd /tmp/local-autoreviewer-pkg && makepkg -si
```

## Releases

Pushing a tag `vX.Y.Z` that matches `version` in `pyproject.toml` runs `.github/workflows/release.yml`. It builds and checks the package, then creates the GitHub release:
- notes from `release-notes/vX.Y.Z.md` plus generated notes;
- `local-autoreviewer-X.Y.Z-1-any.pkg.tar.zst`;
- `SHA256SUMS`.

## Upgrades and rollback

- **Upgrade:** install a newer package. The running worker notices that `/usr/share/local-autoreviewer/VERSION` changed, exits once it is idle, and `Restart=always` brings it back on the new code. The poller always runs the installed version.
- **Roll back:** `pacman -U` an older package. The database schema only grows. If a build finds a schema newer than it supports, it refuses to run and says why; it never rewrites the database.
