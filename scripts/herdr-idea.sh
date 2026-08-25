#!/usr/bin/env bash
# One Herdr *space* per idea in session graphwing. Tab graph stays the dashboard.
set -euo pipefail

SESSION="${GRAPHWING_HERDR_SESSION:-graphwing}"
HOME_DIR="${GRAPHWING_HOME:-$HOME/.graphwing}"
HERDR="${HERDR_BIN:-herdr}"
DASHBOARD_SPACE="graphwing"
RESERVED="graph graphwing"
CMD="open"
LABEL=""
STORY=""
REPO=""
BRANCH=""
BASE="origin/main"
CWD=""
WT_PATH=""
START_CLAUDE=1
USE_WORKTREE=1
FOCUS=1

die() { echo "error: $*" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage:
  graphwing-idea open [--label NAME] [--story SC-N] [--repo NAME|/path]
                      [--branch REF] [--cwd DIR] [--path WORKTREE]
                      [--no-claude] [--no-worktree] [--no-focus]
  graphwing-idea list

Session: herdr --session graphwing (override GRAPHWING_HERDR_SESSION).
Dashboard space "graphwing" / tab "graph" are left alone.
Each idea is a new space. The planning session runs in tab "plan".

Planner: hermes -p riftwing-planner (gpt-5.6-sol) with the grilling skill,
against HERMES_HOME=$GRAPHWING_HOME. Override with GRAPHWING_PLAN_PROFILE,
or GRAPHWING_PLAN_KIND=claude to go back to a Claude pane.
EOF
}

# The grill runs on the planner seat by default. Sol plans; the class table
# keeps Sol out of reviewing so it never grades its own spec.
PLAN_KIND="${GRAPHWING_PLAN_KIND:-hermes}"
PLAN_PROFILE="${GRAPHWING_PLAN_PROFILE:-riftwing-planner}"
if [[ "$PLAN_KIND" == "hermes" ]]; then
  PLAN_TAB="${GRAPHWING_PLAN_TAB:-plan}"
  PLAN_ARGS=(-p "$PLAN_PROFILE" chat -s grilling)
else
  PLAN_TAB="${GRAPHWING_PLAN_TAB:-claude}"
  PLAN_ARGS=()
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    open|list) CMD="$1"; shift ;;
    --label) LABEL="${2:-}"; shift 2 ;;
    --story) STORY="${2:-}"; shift 2 ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --base) BASE="${2:-}"; shift 2 ;;
    --cwd) CWD="${2:-}"; shift 2 ;;
    --path) WT_PATH="${2:-}"; shift 2 ;;
    --no-claude) START_CLAUDE=0; shift ;;
    --no-worktree) USE_WORKTREE=0; shift ;;
    --no-focus) FOCUS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
done

json_get() {
  python3 -c '
import json, os, sys
raw = sys.stdin.read()
i = raw.find("{")
if i < 0:
    sys.exit(1)
d = json.loads(raw[i:])
obj = d.get("result", d)
expr = sys.argv[1]
print(eval(expr, {"obj": obj, "d": d, "os": os}))
' "$1"
}

herdr_s() {
  "$HERDR" --session "$SESSION" "$@"
}

slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/-/g; s/--*/-/g; s/^-//; s/-$//' | cut -c1-32
}

story_num() {
  echo "$1" | sed 's/^[Ss][Cc]-//'
}

resolve_repo() {
  local spec="$1"
  if [[ -z "$spec" ]]; then
    echo ""
    return 0
  fi
  if [[ -d "$spec" ]]; then
    readlink -f "$spec"
    return 0
  fi
  python3 -c '
import json, os, sys
home, name = sys.argv[1], sys.argv[2]
path = os.path.join(home, "repos.json")
try:
    data = json.load(open(path))
except FileNotFoundError:
    sys.exit(3)
repos = data.get("repos", data) if isinstance(data, dict) else {}
if isinstance(repos, list):
    sys.exit(3)
val = repos.get(name)
if not val:
    sys.exit(3)
print(os.path.realpath(val))
' "$HOME_DIR" "$spec"
}

workspace_id_for() {
  local want="$1"
  local raw
  raw="$(herdr_s workspace list)" || return 1
  WANT="$want" python3 -c '
import json, os, sys
raw = sys.stdin.read()
d = json.loads(raw[raw.find("{"):])
want = os.environ["WANT"]
for w in (d.get("result", d).get("workspaces") or []):
    if (w.get("label") or "") == want:
        print(w.get("workspace_id") or "")
        break
' <<<"$raw"
}

ensure_dashboard() {
  local id
  id="$(workspace_id_for "$DASHBOARD_SPACE" || true)"
  if [[ -n "${id:-}" ]]; then
    echo "$id"
    return 0
  fi
  local created pane
  created="$(herdr_s workspace create --cwd "$HOME_DIR" --label "$DASHBOARD_SPACE" --no-focus)"
  id="$(json_get '(obj.get("workspace") or obj).get("workspace_id","")' <<<"$created")"
  pane="$(json_get '((obj.get("root_pane") or obj.get("pane") or {})).get("pane_id","")' <<<"$created")"
  if [[ -n "$pane" ]]; then
    herdr_s tab rename "$(json_get '(obj.get("tab") or {}).get("tab_id","")' <<<"$created")" graph >/dev/null 2>&1 || true
  fi
  echo "$id"
}

tab_id_for() {
  local ws="$1" want="$2"
  local raw
  raw="$(herdr_s tab list --workspace "$ws")" || return 1
  WANT="$want" python3 -c '
import json, os, sys
raw = sys.stdin.read()
d = json.loads(raw[raw.find("{"):])
want = os.environ["WANT"]
for t in (d.get("result", d).get("tabs") or []):
    if (t.get("label") or "") == want:
        print(t.get("tab_id") or "")
        break
' <<<"$raw"
}

pane_has_agent() {
  local pane="$1"
  herdr_s agent list | PANE="$pane" python3 -c '
import json, os, sys
raw = sys.stdin.read()
i = raw.find("{")
if i < 0:
    sys.exit(1)
d = json.loads(raw[i:])
want = os.environ["PANE"]
for a in (d.get("result", d).get("agents") or []):
    if a.get("pane_id") == want:
        sys.exit(0)
sys.exit(1)
'
}

start_claude() {
  local pane="$1" agent="$2"
  if pane_has_agent "$pane"; then
    return 0
  fi
  # New tabs are not a shell prompt yet; agent start fails if we race it.
  herdr_s pane wait-output --source recent-unwrapped --regex '.' --timeout 15000 "$pane" >/dev/null 2>&1 || true

  # The planning seat is the graphwing hermes home, not the personal one: it
  # holds this org's Rewst install, the loop skills, and the riftwing-*
  # profiles. The alias has no HERMES_HOME baked in, so export it in the pane
  # before the agent starts or the session silently uses ~/.hermes instead.
  local seat="${GRAPHWING_HOME:-$HOME/.graphwing}"
  herdr_s pane send-text "$pane" "export HERMES_HOME='$seat'"$'\n' >/dev/null 2>&1 || true
  herdr_s pane wait-output --source recent-unwrapped --regex '.' --timeout 5000 "$pane" >/dev/null 2>&1 || true

  local i err
  err="$(mktemp)"
  for i in 1 2 3 4 5 6 7 8; do
    if herdr_s agent start "$agent" --kind "$PLAN_KIND" --pane "$pane" --timeout 60000 \
        -- "${PLAN_ARGS[@]}" >/dev/null 2>"$err"; then
      rm -f "$err"
      return 0
    fi
    if pane_has_agent "$pane"; then
      rm -f "$err"
      return 0
    fi
    sleep 1
  done
  echo "note: planner did not start. Click space $LABEL, tab $PLAN_TAB, type:" >&2
  echo "  HERMES_HOME='$seat' hermes -p $PLAN_PROFILE chat -s grilling" >&2
  cat "$err" >&2 || true
  rm -f "$err"
}

cmd_list() {
  command -v "$HERDR" >/dev/null || die "herdr not on PATH"
  herdr_s workspace list >/dev/null || die "herdr session $SESSION not reachable (start: herdr --session $SESSION)"
  herdr_s workspace list | python3 -c '
import json, os, sys
raw = sys.stdin.read()
d = json.loads(raw[raw.find("{"):])
dash = os.environ.get("DASHBOARD_SPACE", "graphwing")
for w in (d.get("result", d).get("workspaces") or []):
    lab = w.get("label") or ""
    if lab == dash:
        continue
    print("%s\t%s\t%s tabs" % (w.get("workspace_id"), lab, w.get("tab_count") or 0))
'
}

cmd_open() {
  command -v "$HERDR" >/dev/null || die "herdr not on PATH"
  herdr_s workspace list >/dev/null || die "herdr session $SESSION not reachable (start: herdr --session $SESSION)"

  local story_id=""
  if [[ -n "$STORY" ]]; then
    story_id="$(story_num "$STORY")"
    [[ "$story_id" =~ ^[0-9]+$ ]] || die "--story must look like SC-123 or 123"
  fi
  if [[ -z "$LABEL" ]]; then
    if [[ -n "$story_id" ]]; then
      LABEL="sc-${story_id}"
    else
      die "need --label or --story"
    fi
  fi
  LABEL="$(slugify "$LABEL")"
  [[ -n "$LABEL" ]] || die "empty label"
  for r in $RESERVED; do
    [[ "$LABEL" == "$r" ]] && die "label $LABEL is reserved for the dashboard"
  done
  [[ "$LABEL" == gw-* ]] && die "label $LABEL is reserved for job tabs"

  local repo_path=""
  if [[ -n "$REPO" ]]; then
    repo_path="$(resolve_repo "$REPO")" || die "unknown --repo $REPO (short name in $HOME_DIR/repos.json or a directory)"
  elif [[ -n "$CWD" ]]; then
    repo_path="$(readlink -f "$CWD")"
  fi

  ensure_dashboard >/dev/null

  local ws_id idea_cwd
  ws_id="$(workspace_id_for "$LABEL" || true)"
  idea_cwd="${CWD:-}"
  [[ -n "$idea_cwd" ]] && idea_cwd="$(readlink -f "$idea_cwd")"
  if [[ -z "$idea_cwd" && -n "$repo_path" ]]; then
    idea_cwd="$repo_path"
  fi

  if [[ -z "${ws_id:-}" ]]; then
    if [[ "$USE_WORKTREE" -eq 1 && -n "$repo_path" && -d "$repo_path/.git" ]]; then
      [[ -n "$BRANCH" ]] || {
        if [[ -n "$story_id" ]]; then
          BRANCH="feature/sc-${story_id}/${LABEL}"
        else
          BRANCH="feature/${LABEL}"
        fi
      }
      [[ -n "$WT_PATH" ]] || WT_PATH="$HOME/work/gw-${LABEL}"
      local wt_args=(worktree create --cwd "$repo_path" --branch "$BRANCH" --label "$LABEL" --path "$WT_PATH" --no-focus)
      local created
      if created="$(herdr_s "${wt_args[@]}" 2>/dev/null)"; then
        ws_id="$(json_get '(obj.get("workspace") or obj).get("workspace_id","")' <<<"$created")"
        idea_cwd="$(json_get 'obj.get("path") or obj.get("cwd") or ""' <<<"$created")"
        [[ -n "$idea_cwd" ]] || idea_cwd="$WT_PATH"
      fi
    fi
    if [[ -z "${ws_id:-}" ]]; then
      [[ -n "$idea_cwd" && -d "$idea_cwd" ]] || die "need --cwd or --repo so the space has a directory"
      local created
      created="$(herdr_s workspace create --cwd "$idea_cwd" --label "$LABEL" --no-focus)"
      ws_id="$(json_get '(obj.get("workspace") or obj).get("workspace_id","")' <<<"$created")"
    fi
  fi
  [[ -n "${ws_id:-}" ]] || die "could not create space $LABEL"
  if [[ -z "$idea_cwd" || ! -d "$idea_cwd" ]]; then
    idea_cwd="$(herdr_s workspace get "$ws_id" | json_get 'obj.get("identity_cwd") or obj.get("cwd") or ""')"
  fi
  [[ -n "$idea_cwd" ]] || idea_cwd="${repo_path:-$HOME_DIR}"

  local tab_id pane_id created focus_flag=(--no-focus)
  [[ "$FOCUS" -eq 1 ]] && focus_flag=(--focus)
  tab_id="$(tab_id_for "$ws_id" "$PLAN_TAB" || true)"
  if [[ -z "${tab_id:-}" ]]; then
    created="$(herdr_s tab create --workspace "$ws_id" --cwd "$idea_cwd" --label "$PLAN_TAB" "${focus_flag[@]}")"
    tab_id="$(json_get '(obj.get("tab") or obj).get("tab_id","")' <<<"$created")"
    pane_id="$(json_get '((obj.get("root_pane") or obj.get("pane") or {})).get("pane_id","")' <<<"$created")"
  else
    local panes
    panes="$(herdr_s pane list --workspace "$ws_id")"
    pane_id="$(TAB="$tab_id" python3 -c '
import json, os, sys
raw = sys.stdin.read()
d = json.loads(raw[raw.find("{"):])
tab = os.environ["TAB"]
for p in (d.get("result", d).get("panes") or []):
    if p.get("tab_id") == tab:
        print(p.get("pane_id") or "")
        break
' <<<"$panes")"
    [[ "$FOCUS" -eq 1 ]] && herdr_s tab focus "$tab_id" >/dev/null || true
  fi
  [[ -n "${pane_id:-}" ]] || die "no pane in tab $PLAN_TAB (space $LABEL)"

  local agent
  agent="$(slugify "grill-${LABEL}")"
  [[ "$agent" =~ ^[a-z] ]] || agent="g${agent}"
  agent="$(echo "$agent" | cut -c1-32)"

  if [[ "$START_CLAUDE" -eq 1 ]]; then
    start_claude "$pane_id" "$agent"
  fi

  python3 -c '
import json, sys
print(json.dumps({
  "session": sys.argv[1],
  "dashboard_space": "graphwing",
  "space": sys.argv[2],
  "workspace_id": sys.argv[3],
  "tab": "'"$PLAN_TAB"'",
  "tab_id": sys.argv[4],
  "pane_id": sys.argv[5],
  "cwd": sys.argv[6],
  "agent": sys.argv[7],
  "next": "In tab '"$PLAN_TAB"' start grilling the idea. Do not type in tab graph.",
}, indent=2))
' "$SESSION" "$LABEL" "$ws_id" "${tab_id:-}" "$pane_id" "$idea_cwd" "$agent"
}

case "$CMD" in
  list) DASHBOARD_SPACE="$DASHBOARD_SPACE" cmd_list ;;
  open) cmd_open ;;
  *) die "unknown command $CMD" ;;
esac
