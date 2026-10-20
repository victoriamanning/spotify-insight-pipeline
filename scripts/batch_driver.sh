#!/bin/zsh
# Polls OpenAI batch jobs every 5 minutes, collects finished ones, submits the remaining texts when budget/queue allow.
# Exits when nothing is pending or open, or at the stop time (UTC epoch $1, default +16h).
cd "$(dirname "$0")/.."
stop_at=${1:-$(( $(date +%s) + 57600 ))}
PY=.venv/bin/python
while true; do
  $PY -m pipeline.batch_api collect --run-dir runs/final 2>&1 | grep -E "collected|queue:|Error|Traceback" | sed "s/^/[$(date +%H:%M)] /"
  open=$(sqlite3 runs/final/state.sqlite "select count(*) from batch_jobs where status not in ('collected','failed','expired','cancelled')")
  pending=$(sqlite3 runs/final/state.sqlite "select count(*) from work where status='pending' and attempts=0")
  if [ "$pending" -gt 0 ] && [ "$open" -lt 2 ]; then
    $PY -m pipeline.batch_api submit --run-dir runs/final --texts 110000 2>&1 | tail -1 | sed "s/^/[$(date +%H:%M)] /"
  fi
  open=$(sqlite3 runs/final/state.sqlite "select count(*) from batch_jobs where status not in ('collected','failed','expired','cancelled')")
  if [ "$open" -eq 0 ]; then echo "[$(date +%H:%M)] DONE: no open batch jobs; pending fresh texts: $pending"; exit 0; fi
  if [ "$(date +%s)" -gt "$stop_at" ]; then echo "[$(date +%H:%M)] STOP: time limit reached with $open open jobs"; exit 0; fi
  sleep 300
done
