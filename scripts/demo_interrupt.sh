#!/bin/zsh
# Recorded interruption/resume demo. Part 1: start the 100k enrichment; press Ctrl-C after some batches.
cd "$(dirname "$0")/.."
echo "=== BEFORE: saved queue state ==="; .venv/bin/python -m pipeline status --run-dir runs/final | head -6
echo "=== START enrichment (phase=initial). Press Ctrl-C after a minute ==="
.venv/bin/python -m pipeline enrich --run-dir runs/final --workers 8
echo "=== AFTER INTERRUPT: saved state + checkpoint_before.json ==="
.venv/bin/python -m pipeline status --run-dir runs/final | head -6
.venv/bin/python -c "import json;d=json.load(open('runs/final/checkpoint_before.json'));print('checkpoint_before completed_ids:',d['count'],'saved at',d['saved_at'])"
