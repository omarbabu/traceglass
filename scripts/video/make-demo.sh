#!/usr/bin/env bash
# Record the Traceglass demo video end to end:
#   1. two real, headless Claude Code runs (two models, same task) on a throwaway sample repo
#   2. import + sanitize their sessions, plus the deterministic research demo
#   3. play the scripted tour on a virtual clock and encode 1080p60 MP4 (+ square cut + GIF)
#
# usage: scripts/video/make-demo.sh [--skip-runs]      (needs: claude CLI, Chrome, ffmpeg, npm)
# env:   WORK (default /tmp/traceglass-demo), MODEL_A (claude-opus-5-5), MODEL_B (claude-sonnet-5-5)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="${PY:-$REPO/.venv/bin/python}"
TG=("$PY" -m traceglass)
WORK="${WORK:-/tmp/traceglass-demo}"
MODEL_A="${MODEL_A:-claude-opus-5-5}"
MODEL_B="${MODEL_B:-claude-sonnet-5-5}"
OUT="$REPO/docs/media"
TASK="Checkout is being hammered by client retry storms. Add per-customer rate limiting to checkout: at most 10 requests per customer per rolling 60 seconds; over the limit, return 429 with a Retry-After header. Inject the clock so it's testable, add tests for the limit and the reset, and run the test suite until it passes. Keep the change small."
STYLE="Make every file change with the Edit or Write tools (never shell redirection, heredocs or scripts that write files). Use Bash only to run the tests and inspect the project."
mkdir -p "$WORK" "$OUT"
export TRACEGLASS_HOME="$WORK/tg"

if [[ "${1:-}" != "--skip-runs" ]]; then
  # Hook settings for these runs only (loaded with --settings; your real settings are untouched).
  rm -rf "$WORK/scratch" && mkdir -p "$WORK/scratch" && (cd "$WORK/scratch" &&
    HOME="$WORK/home" CLAUDE_CONFIG_DIR="$WORK/home/.claude" "${TG[@]}" hooks install claude-code --scope local >/dev/null)
  cp "$WORK/scratch/.claude/settings.local.json" "$WORK/hooks.json"
  rm -f "$WORK/video.db"
  for side in a b; do
    model=$([[ $side == a ]] && echo "$MODEL_A" || echo "$MODEL_B")
    dir="$WORK/run-$side"
    rm -rf "$dir" && cp -R "$HERE/demo-project/shop-api" "$dir" && (cd "$dir" && git init -q && git add -A && git -c user.name=demo -c user.email=demo@example.com commit -qm init)
    echo "◆ running $model …"
    (cd "$dir" && claude -p "$TASK" --model "$model" --settings "$WORK/hooks.json" \
      --permission-mode acceptEdits --strict-mcp-config --disable-slash-commands --append-system-prompt "$STYLE" \
      --allowedTools "Read" "Edit" "Write" "Glob" "Grep" "Bash(python3:*)" "Bash(ls:*)" "Bash(cat:*)" "Bash(git diff:*)" \
      --output-format json > "$WORK/run-$side.json")
    session=$("$PY" -c "import json;print(json.load(open('$WORK/run-$side.json'))['session_id'])")
    transcript=$(ls ~/.claude/projects/*/"$session".jsonl | head -1)
    "${TG[@]}" import claude-code "$transcript" --db "$WORK/video.db" >/dev/null
    "${TG[@]}" export "cc_$session" --json --db "$WORK/video.db" -o "$WORK/run-$side.bundle.json"
    "$PY" "$HERE/sanitize.py" "$WORK" "$WORK/run-$side.bundle.json:run-$side" "$WORK/run-$side.clean.json"
  done
  "${TG[@]}" demo research --db "$WORK/video.db" >/dev/null
fi
research=$("$PY" -c "
from traceglass.store import Store; s=Store('$WORK/video.db')
print(next(r['id'] for r in s.list_runs() if r['name'].startswith('Deep research')))")
"${TG[@]}" export "$research" --json --db "$WORK/video.db" -o "$WORK/research.bundle.json"

(cd "$REPO/ui" && npx vite build >/dev/null)
"$PY" "$HERE/build_tour.py" -o "$WORK/tour.html" \
  "$WORK/run-a.clean.json:$WORK/run-a.json" "$WORK/run-b.clean.json:$WORK/run-b.json" "$WORK/research.bundle.json"
(cd "$HERE" && [[ -d node_modules ]] || npm install --silent)
node "$HERE/record.mjs" --url "file://$WORK/tour.html#/?tour=main" \
  --out "$OUT/traceglass-demo.mp4" --gif "$OUT/traceglass-demo.gif" --square --max-seconds 60
echo "✓ $OUT/traceglass-demo.mp4"
