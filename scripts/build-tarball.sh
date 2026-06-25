#!/usr/bin/env bash
# Build a relocatable, self-contained `reachify` tooling tarball for the CURRENT
# OS/arch. The plugin's session-start hook downloads this, extracts it to
# ~/.reachify/tooling/<version>/, and symlinks `reachify` onto PATH.
#
# The bundle contains:
#   python/        a python-build-standalone interpreter (native, relocatable;
#                  fetched via `uv`, copied in as REAL files — no symlinks that
#                  escape the bundle, so it runs on any machine of this OS/arch)
#   lib/           reachify + its deps (click, httpx, …) — all pure-Python,
#                  installed with `uv pip install --target`
#   reachify       a tiny POSIX launcher that resolves through symlinks and runs
#                  `python/bin/python3 -m reachify` with PYTHONPATH=lib
#   VERSION        the packaged version string
#
# Why not a venv: a venv pins its base interpreter by absolute path, so it can't
# be relocated with a bundled Python. Installing pure-Python deps into lib/ and
# invoking the standalone interpreter with `-m` sidesteps that entirely.
#
# Why not PyInstaller: onefile re-extracts ~19 MB to a temp dir on every run and
# macOS Gatekeeper re-scans it each time (~5 s/run). This bundle loads straight
# from disk: ~0.05 s/run.
#
# uv is platform-specific in what it can fetch: run this on macOS for a macOS
# bundle, on Linux for a Linux bundle. CI does all targets (see .github/).
set -euo pipefail

cd "$(dirname "$0")/.."

PYVER="${PYVER:-3.12}"            # bundled Python minor version
OUT_DIR="${OUT_DIR:-dist}"

command -v uv >/dev/null 2>&1 || { echo "error: uv not found (https://docs.astral.sh/uv/)" >&2; exit 1; }

VERSION="$(grep -E '^__version__' src/reachify/__init__.py | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
[ -n "$VERSION" ] || { echo "error: could not read __version__" >&2; exit 1; }

# Normalize host platform to the names the hook will compute from `uname`.
os="$(uname -s)"; arch="$(uname -m)"
case "$os" in Darwin) os=darwin ;; Linux) os=linux ;; *) echo "unsupported OS: $os" >&2; exit 1 ;; esac
case "$arch" in arm64|aarch64) arch=arm64 ;; x86_64|amd64) arch=x64 ;; *) echo "unsupported arch: $arch" >&2; exit 1 ;; esac

name="reachify-${VERSION}-${os}-${arch}"
build="$(mktemp -d)/${name}"
mkdir -p "$build/python"
trap 'rm -rf "$(dirname "$build")"' EXIT

echo "==> Building $name (python $PYVER)"

# 1. Fetch a standalone CPython via uv and copy it in as REAL files.
#    `uv python find` returns a path under a versioned dir that is itself a
#    symlink; resolve the realpath so cp copies files, not a dangling link.
uv python install "$PYVER" >/dev/null 2>&1 || true
# `uv python find` lives under a versioned dir that is itself a symlink; `pwd -P`
# resolves it to the physical path so cp copies real files, not a dangling link.
pbs_real="$(cd "$(dirname "$(uv python find "$PYVER")")/.." && pwd -P)"
echo "==> Bundling interpreter: $pbs_real"
cp -R "$pbs_real/." "$build/python/"

# Safety net: fail if any symlink under python/ points outside the bundle
# (that would break on another machine).
for l in $(find "$build/python" -type l); do
  t="$(readlink "$l")"
  case "$t" in
    /*) case "$t" in "$build"/*) ;; *) echo "error: symlink escapes bundle: $l -> $t" >&2; exit 1 ;; esac ;;
  esac
done

# 2. Install reachify + deps (all pure-Python) into lib/.
echo "==> Installing reachify + deps into lib/"
uv pip install --quiet --python "$build/python/bin/python3" --target "$build/lib" .

# 3. Launcher + version marker.
cat > "$build/reachify" <<'EOF'
#!/bin/sh
# Relocatable reachify launcher. Resolves through symlinks (e.g. a
# /usr/local/bin/reachify -> this file) to find the bundle root, then runs the
# bundled interpreter against the bundled libs. No system Python required.
src="$0"
while [ -h "$src" ]; do
  dir="$(cd "$(dirname "$src")" && pwd)"
  src="$(readlink "$src")"
  case "$src" in /*) ;; *) src="$dir/$src" ;; esac
done
HERE="$(cd "$(dirname "$src")" && pwd)"
exec env PYTHONPATH="$HERE/lib" "$HERE/python/bin/python3" -m reachify "$@"
EOF
chmod +x "$build/reachify"
printf '%s\n' "$VERSION" > "$build/VERSION"

# 4. Tarball — contents extract directly into ~/.reachify/tooling/<version>/.
mkdir -p "$OUT_DIR"
tarball="$OUT_DIR/${name}.tar.gz"
tar -C "$build" -czf "$tarball" .

# 5. Checksum sidecar for the hook to verify the download.
if command -v shasum >/dev/null 2>&1; then
  ( cd "$OUT_DIR" && shasum -a 256 "${name}.tar.gz" > "${name}.tar.gz.sha256" )
else
  ( cd "$OUT_DIR" && sha256sum "${name}.tar.gz" > "${name}.tar.gz.sha256" )
fi

echo
echo "Built:  $tarball  ($(du -h "$tarball" | cut -f1))"
echo "Sha256: $(cut -d' ' -f1 "$tarball.sha256")"
echo
echo "Smoke test:"
smoke="$(mktemp -d)"; tar -C "$smoke" -xzf "$tarball"; "$smoke/reachify" --version; rm -rf "$smoke"
