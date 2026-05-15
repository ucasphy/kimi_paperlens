#!/bin/bash
# Paper-Lens for Kimi Code — Startup Script
set -e

cd "$(dirname "$0")"

export KIMI_CLI_PATH="${KIMI_CLI_PATH:-$(which kimi 2>/dev/null || echo '/home/phyytj/.vscode-server/data/User/globalStorage/moonshot-ai.kimi-code/bin/kimi/kimi')}"
export PAPER_LENS_BACKEND_PORT="${PAPER_LENS_BACKEND_PORT:-8765}"
export PORT="${PORT:-3000}"

# ── Auto cleanup stale processes ──────────────────────────────────────
function cleanup_port() {
    local port=$1
    local pids
    # Try lsof first, then ss, then fuser
    pids=$(lsof -ti :"$port" 2>/dev/null || ss -tlnp 2>/dev/null | grep ":$port " | grep -oP 'pid=\K[0-9]+' | sort -u || true)
    if [ -n "$pids" ]; then
        echo "[cleanup] Killing stale process(es) on port $port: $pids"
        echo "$pids" | xargs -r kill -9 2>/dev/null || true
        sleep 1
    fi
}

cleanup_port "$PAPER_LENS_BACKEND_PORT"
cleanup_port "$PORT"
# Also clean up any lingering next-server processes in this project
pids=$(ps aux | grep "next-server" | grep "$(pwd)/paper-lens-web" | grep -v grep | awk '{print $2}' || true)
if [ -n "$pids" ]; then
    echo "[cleanup] Killing stale next-server process(es): $pids"
    echo "$pids" | xargs -r kill -9 2>/dev/null || true
    sleep 1
fi

# ── Start ─────────────────────────────────────────────────────────────
echo "=============================================="
echo "  Paper-Lens for Kimi Code"
echo "=============================================="
echo "  Kimi CLI: $KIMI_CLI_PATH"
echo "  Backend:  http://localhost:$PAPER_LENS_BACKEND_PORT"
echo "  Frontend: http://localhost:$PORT"
echo "=============================================="
echo ""

cd paper-lens-web
npm run dev
