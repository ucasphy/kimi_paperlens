#!/bin/bash
# Paper-Lens for Kimi Code — Startup Script
set -e

cd "$(dirname "$0")"

export KIMI_CLI_PATH="${KIMI_CLI_PATH:-$(which kimi 2>/dev/null || echo '/home/phyytj/.vscode-server/data/User/globalStorage/moonshot-ai.kimi-code/bin/kimi/kimi')}"
export PAPER_LENS_BACKEND_PORT="${PAPER_LENS_BACKEND_PORT:-8765}"
export PORT="${PORT:-3000}"

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
