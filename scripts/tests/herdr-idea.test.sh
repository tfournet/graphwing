#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$ROOT/scripts/herdr-idea.sh"
FAKE="$(mktemp -d)"
trap 'rm -rf "$FAKE"' EXIT

log="$FAKE/herdr.log"
mkdir -p "$FAKE/bin" "$FAKE/home" "$FAKE/repo"
cat >"$FAKE/home/repos.json" <<EOF
{"scratch": "$FAKE/repo"}
EOF

cat >"$FAKE/bin/herdr" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
LOG="${FAKE_LOG:?}"
echo "$@" >>"$LOG"
args=("$@")
# drop --session NAME
if [[ "${1:-}" == --session ]]; then
  shift 2
fi
case "${1:-} ${2:-}" in
  "workspace list")
    cat <<'JSON'
{"id":"cli:workspace:list","result":{"type":"workspace_list","workspaces":[
  {"workspace_id":"w1","label":"graphwing","tab_count":1},
  {"workspace_id":"w2","label":"sc-109919","tab_count":2}
]}}
JSON
    ;;
  "workspace create")
    cat <<'JSON'
{"id":"cli:workspace:create","result":{"workspace":{"workspace_id":"w9","label":"new"},
  "tab":{"tab_id":"w9:t1"},"root_pane":{"pane_id":"w9:p1"}}}
JSON
    ;;
  "workspace get")
    cat <<'JSON'
{"id":"cli:workspace:get","result":{"workspace_id":"w9","identity_cwd":"/tmp/idea"}}
JSON
    ;;
  "tab list")
    if [[ "$*" == *w2* ]]; then
      cat <<'JSON'
{"id":"cli:tab:list","result":{"tabs":[{"tab_id":"w2:t2","label":"claude"}]}}
JSON
    else
      cat <<'JSON'
{"id":"cli:tab:list","result":{"tabs":[]}}
JSON
    fi
    ;;
  "tab create")
    cat <<'JSON'
{"id":"cli:tab:create","result":{"tab":{"tab_id":"w9:t2"},"root_pane":{"pane_id":"w9:p2"}}}
JSON
    ;;
  "tab focus"|"tab rename")
    echo '{"id":"ok","result":{}}'
    ;;
  "pane list")
    if [[ "$*" == *w2* ]]; then
      cat <<'JSON'
{"id":"cli:pane:list","result":{"panes":[{"pane_id":"w2:p2","tab_id":"w2:t2"}]}}
JSON
    else
      cat <<'JSON'
{"id":"cli:pane:list","result":{"panes":[{"pane_id":"w9:p2","tab_id":"w9:t2"}]}}
JSON
    fi
    ;;
  "worktree create")
    echo "unexpected worktree create" >&2
    exit 1
    ;;
  "agent start")
    echo '{"id":"cli:agent:start","result":{"agent":"grill-x"}}'
    ;;
  "agent list")
    echo '{"id":"cli:agent:list","result":{"agents":[]}}'
    ;;
  "pane wait-output")
    echo '{"id":"cli:pane:wait-output","result":{}}'
    ;;
  *)
    echo "unhandled: $*" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$FAKE/bin/herdr"

export HERDR_BIN="$FAKE/bin/herdr"
export FAKE_LOG="$log"
export GRAPHWING_HOME="$FAKE/home"
export GRAPHWING_HERDR_SESSION="graphwing"
chmod +x "$SCRIPT"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

# 1. reserved dashboard label
if "$SCRIPT" open --label graphwing --no-claude --no-worktree --cwd "$FAKE/repo" 2>/tmp/herdr-idea.err; then
  fail "accepted reserved label graphwing"
fi
grep -q reserved /tmp/herdr-idea.err || fail "missing reserved error"
pass "rejects dashboard label"

if "$SCRIPT" open --label graph --no-claude --no-worktree --cwd "$FAKE/repo" 2>/tmp/herdr-idea.err; then
  fail "accepted reserved label graph"
fi
pass "rejects graph label"

if "$SCRIPT" open --label gw-abc --no-claude --no-worktree --cwd "$FAKE/repo" 2>/tmp/herdr-idea.err; then
  fail "accepted gw- job label"
fi
pass "rejects gw- label"

# 2. list skips dashboard
out="$("$SCRIPT" list)"
echo "$out" | grep -q sc-109919 || fail "list missing idea space"
echo "$out" | grep -q graphwing && fail "list should hide dashboard space"
pass "list hides graphwing space"

# 3. open existing story space, no worktree, no claude — must pass --session
: >"$log"
out="$("$SCRIPT" open --story SC-109919 --no-claude --no-worktree --cwd "$FAKE/repo")"
echo "$out" | grep -q '"space": "sc-109919"' || fail "open story label: $out"
echo "$out" | grep -q '"session": "graphwing"' || fail "session missing: $out"
echo "$out" | grep -q grill-with-docs || fail "missing next hint"
grep -q -- '--session graphwing' "$log" || fail "herdr not called with --session graphwing"
grep -q 'worktree create' "$log" && fail "worktree should be skipped"
pass "open existing story space"

# 4. new label creates workspace, never names it graphwing
: >"$log"
out="$("$SCRIPT" open --label agents-in-projects --no-claude --no-worktree --cwd "$FAKE/repo")"
echo "$out" | grep -q '"space": "agents-in-projects"' || fail "new space: $out"
grep -q 'workspace create' "$log" || fail "expected workspace create"
grep -q 'tab create' "$log" || fail "expected tab create"
pass "open creates idea space + claude tab"

# 5. repo short name from repos.json
: >"$log"
if ! "$SCRIPT" open --label from-repo --repo scratch --no-claude --no-worktree >"$FAKE/from-repo.out" 2>"$FAKE/from-repo.err"; then
  fail "repo short name failed: $(cat "$FAKE/from-repo.err") $(cat "$FAKE/from-repo.out")"
fi
pass "resolves repos.json short name"

# 6. Claude start waits for the shell, then agent start
: >"$log"
"$SCRIPT" open --label wait-shell --no-worktree --cwd "$FAKE/repo" >/dev/null
grep -q 'pane wait-output' "$log" || fail "expected pane wait-output before agent start"
grep -q 'agent start' "$log" || fail "expected agent start"
pass "waits for shell then starts claude"

echo "all herdr-idea tests passed"
