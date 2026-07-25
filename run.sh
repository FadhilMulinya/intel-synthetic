#!/usr/bin/env bash
# One-shot orchestrator: starts the local CKB devnet (if it isn't already
# running) and launches a full simulate.py run detached from the shell, so
# a long --hours run survives the terminal closing.
#
# Assumes offckb and this project's Python dependencies are already
# installed (see README.md / CONTRIBUTING.md for first-time setup).
#
# Usage:
#   ./run.sh --bots 300 --hours 24 --txs 700
#   ./run.sh --bots 100 --hours 12 --txs 200 --out my_run
#
# Output goes to data/<out>/ (default <out> is bot_<bots>), same layout
# simulate.py always writes: bots.json + one add_N.json per bot, plus this
# run's own run.log. Existing run directories are never overwritten.
set -euo pipefail

usage() {
    local exit_code="${1:-1}"
    cat >&2 <<EOF
Usage: $0 --bots N --hours H --txs T [--out NAME]

  --bots N     number of bots to simulate (required)
  --hours H    spread the run over roughly this many hours (required)
  --txs T      transactions per bot (required)
  --out NAME   output folder name under data/ (default: bot_<N>)
EOF
    exit "$exit_code"
}

BOTS=""
HOURS=""
TXS=""
OUT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bots) BOTS="${2:-}"; shift 2 ;;
        --hours) HOURS="${2:-}"; shift 2 ;;
        --txs) TXS="${2:-}"; shift 2 ;;
        --out) OUT="${2:-}"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "error: unknown argument: $1" >&2; usage ;;
    esac
done

[[ -n "$BOTS" && -n "$HOURS" && -n "$TXS" ]] || usage
[[ "$BOTS" =~ ^[0-9]+$ ]] || { echo "error: --bots must be a positive integer" >&2; exit 1; }
[[ "$TXS" =~ ^[0-9]+$ ]] || { echo "error: --txs must be a positive integer" >&2; exit 1; }
[[ "$HOURS" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "error: --hours must be a number" >&2; exit 1; }

OUT="${OUT:-bot_${BOTS}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
OUT_DIR="$DATA_DIR/$OUT"

if [[ -e "$OUT_DIR" ]]; then
    echo "error: $OUT_DIR already exists -- refusing to overwrite past run data." >&2
    echo "       pass --out with a different name to start a new run." >&2
    exit 1
fi

command -v offckb >/dev/null 2>&1 || { echo "error: offckb not found on PATH" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found on PATH" >&2; exit 1; }

RPC_URL="http://127.0.0.1:8114"

devnet_up() {
    curl -s -X POST -H 'Content-Type: application/json' \
        -d '{"id":1,"jsonrpc":"2.0","method":"get_tip_block_number","params":[]}' \
        "$RPC_URL" >/dev/null 2>&1
}

mkdir -p "$DATA_DIR"

if devnet_up; then
    echo "devnet already running at $RPC_URL"
else
    echo "starting devnet (existing chain data, if any, is reused -- not wiped)..."
    nohup offckb node > "$DATA_DIR/devnet.log" 2>&1 &
    disown
    up=0
    for _ in $(seq 1 60); do
        if devnet_up; then
            up=1
            break
        fi
        sleep 1
    done
    [[ "$up" -eq 1 ]] || { echo "error: devnet did not come up within 60s (see data/devnet.log)" >&2; exit 1; }
    echo "devnet is up."
fi

mkdir -p "$OUT_DIR"
LOG_FILE="$OUT_DIR/run.log"

echo "launching: $BOTS bots, $TXS txs/bot, spread over ~${HOURS}h -> data/$OUT/"
cd "$SCRIPT_DIR"
nohup python3 -u simulate.py --bots "$BOTS" --txs-per-bot "$TXS" --out "$OUT" --duration-hours "$HOURS" \
    > "$LOG_FILE" 2>&1 &
disown

echo "started in background (pid $!)."
echo "tail -f data/$OUT/run.log        # watch progress"
echo "wc -l data/$OUT/add_*.json       # count transactions so far"
