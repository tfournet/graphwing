#!/usr/bin/env bash
# graphwing on a clean machine: optional Hermes/herdr/cloudflared, then start the API.
set -euo pipefail

GRAPHWING_REPO_URL="${GRAPHWING_REPO_URL:-https://github.com/tfournet/graphwing.git}"
HERMES_INSTALL_URL="${HERMES_INSTALL_URL:-https://hermes-agent.nousresearch.com/install.sh}"
HERDR_INSTALL_URL="${HERDR_INSTALL_URL:-https://herdr.dev/install.sh}"

export PATH="${HOME}/.local/bin:${PATH}"

log() { printf 'graphwing: %s\n' "$*"; }
die() { printf 'graphwing: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<'EOF'
Usage: start.sh [options]

Clean-machine getting started. From a clone:

  ./start.sh

Or on an empty box:

  curl -fsSL https://raw.githubusercontent.com/tfournet/graphwing/main/start.sh | bash -s -- --yes

Options:
  --yes                 no prompts (skips optional extras unless --with-*)
  --with-hermes         install Hermes Agent if missing (agentRun)
  --no-hermes           never install Hermes
  --with-herdr          install herdr if missing
  --no-herdr            never install herdr
  --tunnel none|demo|named   default none when --yes
  --with-cloudflared    install cloudflared if missing (implied by demo/named)
  --home DIR            GRAPHWING_HOME (default ~/.graphwing)
  --unit-dir DIR        systemd user unit dir
  --no-units            do not write systemd units
  --no-cli              do not install ~/.local/bin/graphwing or graphwing-idea
  --port N              listen port (default 8645)
  --foreground          run the API in this terminal (default)
  --daemon              enable systemd --user units instead
  --no-start            install only
  --repo NAME=PATH      allowlist a git checkout (repeatable)
  -h, --help
EOF
}

need() {
  have "$1" || die "need $1 on PATH"
}

ask() {
  local prompt="$1" default="$2" val=""
  if [[ "$YES" == 1 ]]; then
    printf '%s\n' "$default"
    return
  fi
  read -r -p "$prompt [$default]: " val || true
  printf '%s\n' "${val:-$default}"
}

yes() {
  local prompt="$1" default="$2" val
  val="$(ask "$prompt" "$default")"
  case "${val,,}" in
    y|yes|1|true) return 0 ;;
    *) return 1 ;;
  esac
}

is_repo() {
  [[ -f "$1/server.py" && -f "$1/install.py" && -f "$1/openapi.json" ]]
}

YES=0
WITH_HERMES=auto
WITH_HERDR=auto
WITH_CLOUDFLARED=auto
TUNNEL=auto
START=foreground
HOME_DIR="${GRAPHWING_HOME:-$HOME/.graphwing}"
UNIT_DIR=""
NO_UNITS=0
NO_CLI=0
PORT="${GRAPHWING_PORT:-8645}"
REPOS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --with-hermes) WITH_HERMES=yes ;;
    --no-hermes) WITH_HERMES=no ;;
    --with-herdr) WITH_HERDR=yes ;;
    --no-herdr) WITH_HERDR=no ;;
    --with-cloudflared) WITH_CLOUDFLARED=yes ;;
    --no-cloudflared) WITH_CLOUDFLARED=no ;;
    --tunnel)
      shift
      TUNNEL="${1:-}"
      [[ "$TUNNEL" == none || "$TUNNEL" == demo || "$TUNNEL" == named ]] || die "tunnel must be none, demo, or named"
      ;;
    --home)
      shift
      HOME_DIR="${1:-}"
      [[ -n "$HOME_DIR" ]] || die "--home needs a directory"
      ;;
    --unit-dir)
      shift
      UNIT_DIR="${1:-}"
      ;;
    --no-units) NO_UNITS=1 ;;
    --no-cli) NO_CLI=1 ;;
    --port)
      shift
      PORT="${1:-}"
      ;;
    --foreground) START=foreground ;;
    --daemon) START=daemon ;;
    --no-start) START=no ;;
    --repo)
      shift
      [[ -n "${1:-}" ]] || die "--repo needs NAME=PATH"
      REPOS+=("$1")
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
  shift
done

if [[ "$YES" == 0 && ! -t 0 ]]; then
  YES=1
  log "stdin is not a TTY; non-interactive defaults (pass --with-hermes etc. if you want extras)"
fi

if [[ "$YES" == 1 ]]; then
  [[ "$WITH_HERMES" == auto ]] && WITH_HERMES=no
  [[ "$WITH_HERDR" == auto ]] && WITH_HERDR=no
  [[ "$WITH_CLOUDFLARED" == auto ]] && WITH_CLOUDFLARED=no
  [[ "$TUNNEL" == auto ]] && TUNNEL=none
fi

HERE=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

REPO=""
if [[ -n "$HERE" ]] && is_repo "$HERE"; then
  REPO="$HERE"
else
  dest="${GRAPHWING_SRC:-$HOME/graphwing}"
  if is_repo "$dest"; then
    REPO="$dest"
  else
    need git
    log "cloning $GRAPHWING_REPO_URL -> $dest"
    git clone --depth 1 "$GRAPHWING_REPO_URL" "$dest"
    is_repo "$dest" || die "clone at $dest is missing catalog files"
    REPO="$dest"
  fi
  if [[ -z "$HERE" || "$HERE" != "$REPO" ]]; then
    relaunch=(bash "$REPO/start.sh")
    [[ "$YES" == 1 ]] && relaunch+=(--yes)
    [[ "$WITH_HERMES" == yes ]] && relaunch+=(--with-hermes)
    [[ "$WITH_HERMES" == no ]] && relaunch+=(--no-hermes)
    [[ "$WITH_HERDR" == yes ]] && relaunch+=(--with-herdr)
    [[ "$WITH_HERDR" == no ]] && relaunch+=(--no-herdr)
    [[ "$WITH_CLOUDFLARED" == yes ]] && relaunch+=(--with-cloudflared)
    [[ "$WITH_CLOUDFLARED" == no ]] && relaunch+=(--no-cloudflared)
    relaunch+=(--tunnel "${TUNNEL:-none}" --home "$HOME_DIR" --port "$PORT")
    [[ -n "$UNIT_DIR" ]] && relaunch+=(--unit-dir "$UNIT_DIR")
    [[ "$NO_UNITS" == 1 ]] && relaunch+=(--no-units)
    [[ "$NO_CLI" == 1 ]] && relaunch+=(--no-cli)
    [[ "$START" == foreground ]] && relaunch+=(--foreground)
    [[ "$START" == daemon ]] && relaunch+=(--daemon)
    [[ "$START" == no ]] && relaunch+=(--no-start)
    for spec in "${REPOS[@]+"${REPOS[@]}"}"; do
      relaunch+=(--repo "$spec")
    done
    exec "${relaunch[@]}"
  fi
fi

need python3
have git || log "warning: git not on PATH (git nodes will 501)"
have curl || log "warning: curl not on PATH (optional fetches disabled)"

if [[ "$WITH_HERMES" == auto ]]; then
  if have hermes || [[ -x "$HOME/.local/bin/hermes" ]]; then
    WITH_HERMES=no
    log "hermes already on PATH"
  elif yes "Install Hermes Agent (needed for POST /v1/agent/run)?" "Y"; then
    WITH_HERMES=yes
  else
    WITH_HERMES=no
  fi
fi

if [[ "$WITH_HERDR" == auto ]]; then
  if have herdr || [[ -x "$HOME/.local/bin/herdr" ]]; then
    WITH_HERDR=no
    log "herdr already on PATH"
  elif yes "Install herdr (human session)?" "n"; then
    WITH_HERDR=yes
  else
    WITH_HERDR=no
  fi
fi

if [[ "$TUNNEL" == auto ]]; then
  if have cloudflared || [[ -x "$HOME/.local/bin/cloudflared" ]]; then
    TUNNEL="$(ask "Tunnel mode none/demo/named" "demo")"
  else
    TUNNEL="$(ask "Tunnel mode none/demo/named" "none")"
  fi
  case "$TUNNEL" in
    none|demo|named) ;;
    *) die "tunnel must be none, demo, or named" ;;
  esac
fi

if [[ "$TUNNEL" != none && "$WITH_CLOUDFLARED" == auto ]]; then
  if have cloudflared || [[ -x "$HOME/.local/bin/cloudflared" ]]; then
    WITH_CLOUDFLARED=no
  else
    WITH_CLOUDFLARED=yes
  fi
fi
[[ "$WITH_CLOUDFLARED" == auto ]] && WITH_CLOUDFLARED=no

already() {
  have "$1" || [[ -x "$HOME/.local/bin/$1" ]]
}

install_hermes() {
  if already hermes; then
    return 0
  fi
  have curl || die "curl required to install Hermes"
  log "installing Hermes Agent from $HERMES_INSTALL_URL"
  curl -fsSL "$HERMES_INSTALL_URL" | bash -s -- --skip-browser --skip-computer-use
  export PATH="${HOME}/.local/bin:${PATH}"
  already hermes || die "hermes install finished but hermes is not on PATH (try: export PATH=\"\$HOME/.local/bin:\$PATH\")"
}

install_herdr() {
  if already herdr; then
    return 0
  fi
  have curl || die "curl required to install herdr"
  log "installing herdr from $HERDR_INSTALL_URL"
  curl -fsSL "$HERDR_INSTALL_URL" | sh
  export PATH="${HOME}/.local/bin:${PATH}"
  already herdr || die "herdr install finished but herdr is not on PATH"
}

install_cloudflared() {
  if already cloudflared; then
    return 0
  fi
  have curl || die "curl required to install cloudflared"
  local os arch asset dest
  os="$(uname -s)"
  arch="$(uname -m)"
  asset=""
  case "$os-$arch" in
    Linux-x86_64|Linux-amd64) asset=cloudflared-linux-amd64 ;;
    Linux-aarch64|Linux-arm64) asset=cloudflared-linux-arm64 ;;
  esac
  [[ -n "$asset" ]] || die "install cloudflared yourself (no binary mapping for $os $arch)"
  dest="$HOME/.local/bin/cloudflared"
  mkdir -p "$(dirname "$dest")"
  log "installing $asset -> $dest"
  curl -fsSL -o "$dest" "https://github.com/cloudflare/cloudflared/releases/latest/download/${asset}"
  chmod +x "$dest"
}

[[ "$WITH_HERMES" == yes ]] && install_hermes
[[ "$WITH_HERDR" == yes ]] && install_herdr
[[ "$WITH_CLOUDFLARED" == yes ]] && install_cloudflared

HOME_DIR="$(python3 -c 'import os,sys; from pathlib import Path; print(Path(sys.argv[1]).expanduser().resolve())' "$HOME_DIR")"
export GRAPHWING_HOME="$HOME_DIR"
export GRAPHWING_PORT="$PORT"

install_args=(--home "$HOME_DIR" --tunnel "$TUNNEL" --port "$PORT")
[[ "$YES" == 1 ]] && install_args+=(--non-interactive)
for spec in "${REPOS[@]+"${REPOS[@]}"}"; do
  install_args+=(--repo "$spec")
done
[[ -n "$UNIT_DIR" ]] && install_args+=(--unit-dir "$UNIT_DIR")
[[ "$NO_UNITS" == 1 ]] && install_args+=(--no-units)
[[ "$NO_CLI" == 1 ]] && install_args+=(--no-cli)
[[ "$START" == daemon && "$NO_UNITS" != 1 ]] && install_args+=(--start)

log "installing catalog into $HOME_DIR"
python3 "$REPO/install.py" "${install_args[@]}"

key_file="$HOME_DIR/api.key"
log "API http://127.0.0.1:${PORT}"
log "header X-Graphwing-Key from $key_file (or GRAPHWING_KEY)"
log "agentRun needs hermes; skipped extras stay optional"

if [[ "$START" == no ]]; then
  log "not starting (--no-start)"
  exit 0
fi

if [[ "$START" == daemon ]]; then
  log "units enabled; API should be on http://127.0.0.1:${PORT}"
  exit 0
fi

log "starting API in this terminal (Ctrl-C stops it)"
cd "$HOME_DIR"
exec python3 "$HOME_DIR/server.py"
