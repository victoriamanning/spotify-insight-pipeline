#!/bin/zsh
# Part 2: resume. Already-completed IDs are not re-sent; new work is logged with phase=resume.
cd "$(dirname "$0")/.."
echo "=== RESUME (phase=resume): only pending texts are dispatched ==="
.venv/bin/python -m pipeline enrich --run-dir runs/final --workers 8
.venv/bin/python -m pipeline status --run-dir runs/final | head -6
.venv/bin/python -c "
import json;b=set(json.load(open('runs/final/checkpoint_before.json'))['completed_ids']);a=set(json.load(open('runs/final/checkpoint_after.json'))['completed_ids'])
calls=[json.loads(l) for l in open('runs/final/calls.jsonl')];res=[c for c in calls if c['role']=='enrich' and c['phase']=='resume']
print('before',len(b),'after',len(a),'new completed',len(a-b),'| resume calls',len(res),'| resume calls touching checkpoint_before IDs:',sum(bool(b & set(c['review_ids'])) for c in res))"
