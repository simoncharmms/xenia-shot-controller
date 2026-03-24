#!/usr/bin/env bash
# Xenia Shot Controller — launch script
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Kill any existing instance cleanly
pkill -f "controller.py" 2>/dev/null && sleep 0.5 || true
# Belt-and-suspenders: free the ports
for PORT in 8765 8766; do
  fuser -k "${PORT}/tcp" 2>/dev/null || true
done

# Create venv if needed
if [ ! -d ".venv" ]; then
  echo "→ Creating virtual environment..."
  python3 -m venv .venv
fi

# Activate
source .venv/bin/activate

# Install deps
echo "→ Installing dependencies..."
pip install -q -r requirements.txt

# Ensure data dir
mkdir -p data
[ -f data/shots.json ] || echo '[]' > data/shots.json

# Launch
echo ""
echo "╔══════════════════════════════════════╗"
echo "║   XENIA SHOT CONTROLLER              ║"
echo "║   UI  → http://localhost:8766        ║"
echo "║   WS  → ws://localhost:8765          ║"
echo "╚══════════════════════════════════════╝"
echo ""

if [[ "$1" == "--demo" ]]; then
  echo "🎭 Starting in DEMO mode (simulated shot, no machine needed)"
  echo ""
  python3 controller.py --demo
else
  echo "🔌 Connecting to Xenia at 192.168.2.102"
  echo "   (Pass --demo to run without machine)"
  echo ""
  python3 controller.py
fi
