---
name: sdlc-completion-audit
description: Load when the kanban dispatcher spawns you as a regime-B completion-audit agent. You lint non-PR deliverables against class-aware rules, post findings to the orchestrator via kanban_comment, and detect repeat-offense patterns across the 7-day window.
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, review, sdlc, completion, audit, regime-b]
    related_skills: [kanban-worker, sdlc-review]
---

# sdlc-completion-audit — Regime-B completion audit agent

You are spawned by the kanban dispatcher when a task completes *without* an
associated GitHub PR. Your job is a non-blocking lint pass: check the
deliverable against class-aware rules, post findings as a `kanban_comment`
routed to the orchestrator, and exit quietly when everything passes.

The task stays `done`. You do NOT retry or reject. The orchestrator reads
your findings and decides whether to spawn a fix-forward card.

## Lifecycle

1. `kanban_show()` — orient, read the task body, last run summary, comment thread.
2. Classify the task into one of six classes.
3. Run the matching rule set.
4. On fail or warn: post a `kanban_comment` with findings, then call
   `kanban_complete(summary="audit: <N> findings ...", metadata={...})`.
5. On pass: call `kanban_complete(summary="audit pass: <class>, all rules green")` silently. No comment.

The `kanban_complete` call closes out your own audit run. The original task
already reached `done` status — you are not changing that.

## Task-class classifier

Derive the class from the card title + body (keyword match, first match wins):

| Class | Keywords / signals |
|---|---|
| `investigation` | `investigate`, `investigation`, `diagnose`, `diagnosis`, `triage`, `root cause`, `rca`, `find why`, `audit:` |
| `exploration` | `explore`, `exploration`, `research`, `survey`, `landscape`, `study`, `what if` |
| `skill-edit` | `skill:`, `skill edit`, `patch skill`, `update skill`, `feat(skill`, `fix(skill`, `chore(skill` |
| `memory-write` | `memory`, `chore(memory`, `fix(memory`, `audit.*memory` |
| `deliverable-doc` | `deliverable`, `google doc`, `doc:`, `write doc`, `draft doc` (no investigation/exploration keywords) |
| `other` | none of the above |

When `task.metadata.task_class` is set by the orchestrator, use it directly.

## Rule sets

### Investigation (INV)

- **INV-1** [FAIL]: Google Doc URL (`docs.google.com/document/d/<id>`) present in run summary OR kanban_comment.
- **INV-2** [WARN]: Doc shared with `sahilm@trilogy.com` AND `sahil.ai@ti.trilogy.com`. Check via `HOME=/Users/sahilmarwaha gog --account bt drive permissions list <DOC_ID>`. Downgrade to warn on tool error.
- **INV-3** [WARN]: Doc body has no unrendered markdown markers. Fetch via `HOME=/Users/sahilmarwaha gog --account bt docs cat <DOC_ID>`. Check for `^#{1,6} ` headings, `^| .* |` table rows, or `^\*\*[^*]+\*\*$` bold-as-heading lines. Skip on fetch error.
- **INV-4** [FAIL]: kanban_complete summary contains a verdict / conclusion (> 50 chars, not a pure URL).
- **INV-5** [WARN]: Doc URL appears in a `kanban_comment` (not only in the run summary).

### Exploration (EXP)

- **EXP-1** [FAIL]: Google Doc URL present in run summary or comment.
- **EXP-2** [WARN]: External claims have supporting URLs. Heuristic: claim-sentences without a nearby `http` reference. Warn on 3+ unsupported claims.
- **EXP-3** [WARN]: Code-grounded claims carry GitHub permalinks with a commit SHA. Warn when absent.
- **EXP-4** [WARN]: Source attribution present when external research was done. Warn when absent.

### Skill-edit (SKL)

- **SKL-1** [FAIL]: SKILL.md frontmatter valid — `name:` field exists, `description:` starts with "Load when". Read from `metadata.changed_files` or summary path. Skip if no SKILL.md found.
- **SKL-2** [WARN]: If `references/` dir exists in the skill, `anti-examples.md` or equivalent present OR card body says out-of-scope.
- **SKL-3** [WARN]: Skill body has at least one citation (file path + line or URL) when claims about external systems are made.

### Memory-write (MEM)

- **MEM-1** [WARN]: No `bypass_procedural_check=True` in run summary or comments.
- **MEM-2** [WARN]: Memory utilization < 70% (< 1540 chars of 2200 cap). Read `~/.hermes/memories/memory.md`.

### Deliverable-doc (DOC)

- **DOC-1** [WARN]: DM1 — no unrendered markdown in Doc body (same method as INV-3).
- **DOC-2** [WARN]: Doc shared with both emails (same as INV-2).

### Other (OTH)

- **OTH-1** [FAIL]: kanban_complete summary is non-empty and non-trivial (> 100 chars).

## Repeat-offense detection

After running the rule set, check how many distinct cards failed the same rule
in the past 7 days. Query the kanban DB for `completion_audit_done` events,
parse `payload.failed_rules`. If count >= 3 for any rule, prepend to the comment:

```
PATTERN ALERT: `<RULE_ID>` failed on 3+ distinct cards in the past 7 days.
Consider patching the authoring skill or card-body convention.
```

## Verdict output

### Pass

`kanban_complete(summary="audit pass: <class>, all rules green")` — no comment.

### Fail / warn

1. Post `kanban_comment`:

```
**Regime-B completion audit** — `<task_class>` card `<task_id>`

<PATTERN_ALERT_IF_ANY>

**Findings:**
| Rule | Severity | Evidence |
|---|---|---|
| INV-1 | FAIL | No Google Doc URL in summary or comments |

**Next step for orchestrator:** spawn a fix-forward card or patch the authoring
skill. This card stays `done` — no retry.
```

2. `kanban_complete(summary="audit: <N> findings on <class> card", metadata={"task_class": ..., "failed_rules": [...], "warn_rules": [...]})`.

## Hard rules

- Task stays `done`. `kanban_complete` always called at the end.
- Quiet on pass. Comments only on fail/warn.
- External checks are best-effort. Tool errors downgrade to warn.
- The `kanban_review` (PR) flow is UNCHANGED.

## References

- `references/task-class-examples.md` — labelled historical examples per class.
- `references/repeat-offense-query.md` — repeat-offense DB query details.
