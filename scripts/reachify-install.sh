#!/usr/bin/env bash
# Reference installer for the reachify tooling bundle.
#
# This is the logic the PLUGIN's session-start hook should run (copy it into the
# plugin repo, or curl it from a pinned tag). It is idempotent and fast on the
# hot path: if the requested version is already unpacked, it only re-points the
# symlink and exits in milliseconds.
#
# What it does:
#   1. Resolve version + host platform (darwin/linux × arm64/x64).
#   2. If ~/.reachify/tooling/<version>/ is missing, download the matching
#      tarball from the GitHub release, verify its sha256, and extract it.
#   3. Symlink `reachify` onto PATH pointing at that version's launcher.
#
# Configure via env (or edit the defaults):
#   REACHIFY_REPO     GitHub "owner/repo" that hosts the release assets
#   REACHIFY_VERSION  version to install (e.g. 0.0.1) — pin this in the plugin
#   REACHIFY_BINDIR   where to place the `reachify` symlink (auto-detected)
set -euo pipefail

REPO="adnrs96/reachify-cli"
VERSION="${REACHIFY_VERSION:?set REACHIFY_VERSION to the version to install}"
HOME_BASE="${REACHIFY_HOME:-$HOME/.reachify}"
ROOT="$HOME_BASE/tooling/$VERSION"

# --- platform detection (must match scripts/build-tarball.sh naming) ---------
os="$(uname -s)"; arch="$(uname -m)"
case "$os" in Darwin) os=darwin ;; Linux) os=linux ;; *) echo "reachify: unsupported OS $os" >&2; exit 1 ;; esac
case "$arch" in arm64|aarch64) arch=arm64 ;; x86_64|amd64) arch=x64 ;; *) echo "reachify: unsupported arch $arch" >&2; exit 1 ;; esac
asset="reachify-${VERSION}-${os}-${arch}.tar.gz"

# --- choose where to put the `reachify` symlink ------------------------------
# We install ONLY into a conventional command directory — one of the standard
# spots a CLI symlink is *meant* to live (/usr/local/bin and friends) — never
# just any writable dir that happens to be early on PATH (e.g. ./node_modules/.bin,
# a project shim dir, or "."). Among those conventional dirs we prefer one that
# is already on PATH, so `reachify` resolves to this bundle without extra setup.
#
# NOTE: /usr/bin is deliberately NOT a candidate — it's SIP-protected on macOS
# (read-only) and the wrong place for user-installed commands anyway.
choose_bindir() {
  if [ -n "${REACHIFY_BINDIR:-}" ]; then printf '%s\n' "$REACHIFY_BINDIR"; return; fi

  # Conventional install dirs for user-managed command symlinks, preferred first:
  #   /usr/local/bin    classic system-wide spot (Intel Homebrew / manual installs)
  #   /opt/homebrew/bin Homebrew on Apple Silicon
  #   ~/.local/bin      XDG user-level bin (pip --user / pipx); always user-writable
  #   ~/bin             legacy user bin
  set -- /usr/local/bin /opt/homebrew/bin "$HOME/.local/bin" "$HOME/bin"

  # Pass 1: a conventional dir that is writable AND already on PATH.
  for d in "$@"; do
    [ -d "$d" ] && [ -w "$d" ] || continue
    case ":$PATH:" in *":$d:"*) printf '%s\n' "$d"; return ;; esac
  done
  # Pass 2: a conventional dir that is writable (even if not yet on PATH).
  for d in "$@"; do
    [ -d "$d" ] && [ -w "$d" ] && { printf '%s\n' "$d"; return; }
  done
  # Pass 3: default to ~/.local/bin (created below; PATH warning emitted later).
  printf '%s\n' "$HOME/.local/bin"
}
BINDIR="$(choose_bindir)"

# Resolve a symlink chain to its final path (pure shell, no python/realpath).
resolve() {
  p="$1"
  while [ -L "$p" ]; do
    t="$(readlink "$p")"
    case "$t" in /*) p="$t" ;; *) p="$(cd "$(dirname "$p")" && pwd)/$t" ;; esac
  done
  printf '%s\n' "$p"
}

# --- download + extract only if this version isn't already present -----------
if [ ! -x "$ROOT/reachify" ]; then
  echo "reachify: installing $VERSION ($os-$arch) ..." >&2
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  base="https://github.com/$REPO/releases/download/v$VERSION"
  curl -fsSL "$base/$asset"        -o "$tmp/$asset"
  curl -fsSL "$base/$asset.sha256" -o "$tmp/$asset.sha256" || true

  if [ -s "$tmp/$asset.sha256" ]; then
    want="$(cut -d' ' -f1 "$tmp/$asset.sha256")"
    if command -v shasum >/dev/null 2>&1; then got="$(shasum -a 256 "$tmp/$asset" | cut -d' ' -f1)";
    else got="$(sha256sum "$tmp/$asset" | cut -d' ' -f1)"; fi
    [ "$want" = "$got" ] || { echo "reachify: checksum mismatch for $asset" >&2; exit 1; }
  fi

  # Extract atomically: unpack to a temp sibling, then rename into place.
  mkdir -p "$(dirname "$ROOT")"
  staging="$ROOT.tmp.$$"; rm -rf "$staging"; mkdir -p "$staging"
  tar -C "$staging" -xzf "$tmp/$asset"
  rm -rf "$ROOT"; mv "$staging" "$ROOT"
fi

# --- point the canonical symlink ---------------------------------------------
mkdir -p "$BINDIR"
ln -sfn "$ROOT/reachify" "$BINDIR/reachify"

# Sweep PATH for any OTHER `reachify` that could win the lookup. Repoint the ones
# that are our own older symlinks (into ~/.reachify); flag anything foreign so a
# stale/conflicting tool can never be conflated with this bundle.
target="$ROOT/reachify"
foreign=""
oldifs="$IFS"; IFS=:
for d in $PATH; do
  [ -n "$d" ] || continue
  [ "$d" = "$BINDIR" ] && continue
  cand="$d/reachify"
  [ -e "$cand" ] || [ -L "$cand" ] || continue
  if [ -L "$cand" ] && case "$(readlink "$cand")" in "$HOME_BASE"/*) true ;; *) false ;; esac; then
    # Our own previous install — repoint at the current version if we can.
    [ -w "$d" ] && ln -sfn "$target" "$cand" || foreign="$foreign $cand"
  else
    foreign="$foreign $cand"   # a different tool/binary named reachify
  fi
done
IFS="$oldifs"

# Verify the shell will actually pick OURS, and surface anything in the way.
active="$(command -v reachify 2>/dev/null || true)"
if [ -n "$active" ] && [ "$(resolve "$active")" = "$(resolve "$target")" ]; then
  echo "reachify $VERSION ready: $active -> $target" >&2
else
  echo "reachify: WARNING — 'reachify' resolves to '${active:-nothing}', not this bundle." >&2
  [ -n "$foreign" ] && echo "reachify: conflicting entries on PATH:$foreign — remove them." >&2
  case ":$PATH:" in *":$BINDIR:"*) ;; *) echo "reachify: also note — $BINDIR is not on PATH." >&2 ;; esac
fi
