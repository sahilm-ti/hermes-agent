# Repeat-offense detection query

## Purpose

After running the per-class rule set, the audit agent checks how often the
same rule has failed in the last 7 days. Three or more distinct cards failing
the same rule triggers a PATTERN ALERT in the comment, prompting the
orchestrator to fix the authoring skill root cause rather than spawning
individual fix-forward cards.

## Query

```python
import sqlite3, json, time

conn = sqlite3.connect("/Users/sahilmarwaha/.hermes/kanban.db")
conn.row_factory = sqlite3.Row

cutoff = int(time.time()) - 7 * 24 * 3600
rows = conn.execute(
    "SELECT task_id, payload FROM task_events "
    "WHERE kind = 'completion_audit_done' AND created_at > ? "
    "ORDER BY created_at DESC",
    (cutoff,),
).fetchall()

rule_failures = {}  # rule_id -> set of task_ids
for row in rows:
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (ValueError, TypeError):
        continue
    for rule_id in payload.get("failed_rules", []):
        rule_failures.setdefault(rule_id, set()).add(row["task_id"])

# Pattern alert for any rule with 3+ distinct failing cards
pattern_alerts = [
    rule_id
    for rule_id, task_ids in rule_failures.items()
    if len(task_ids) >= 3
]
```

## Notes

- The query targets `completion_audit_done` events (written by the audit agent
  on every run, pass or fail) rather than `kanban_comment` events (which would
  miss silent passes).
- The `payload.failed_rules` list is the machine-readable output from the audit
  agent's `kanban_complete(metadata={"failed_rules": [...]})` call.
- Pattern alerts fire at 3+ distinct card IDs (not 3+ events) to avoid false
  positives from re-audits of the same card.
