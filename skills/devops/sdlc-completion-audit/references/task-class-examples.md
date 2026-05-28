# Task-class labelled examples

Historical `kanban_complete` cards from the past 30 days, manually classified.
Use these to smoke-test the classifier before shipping.

## investigation

- `t_b0e9a537` — "investigation: does Google's new user-auth cardsV2 API actually help us?"
  Completed via `kanban_complete` with run summary pointing to a follow-up card.
  **INV-1 FAIL**: no Google Doc URL in summary or comments (findings were in a kanban_comment).
  **INV-4 PASS**: summary has a verdict.

- `t_a83ff71d` — "port t_b0e9a537 investigation comment to a Google Doc and share with Sahil"
  Summary: "Investigation ported to Google Doc. URL: https://docs.google.com/..."
  **INV-1 PASS**: Doc URL in summary.
  **INV-4 PASS**: summary has a conclusion.

- `t_aa83e513` — "round 2: code-grounded verification of hive debugging playbook"
  Completed without a PR.
  Expected class: `investigation`.

## skill-edit

- `t_aa450c9d` — "skill: workers must declare screenshot omissions in PR description"
  Status: `human_review` (PR path — should NOT be audited; has PR).
  Expected: **skipped by regime-B** (PR present).

- `t_74e85a77` — "sdlc-review: drop the pr-skill without executables exception"
  Completed without a PR (skill was edited in-place).
  **SKL-1**: check the SKILL.md frontmatter.
  **SKL-3**: skill references to external files should have citations.

## memory-write

- `t_7a4359e0` — "chore(memory): audit braintrustorch memory.md + user.md"
  **MEM-1**: check for bypass_procedural_check.
  **MEM-2**: check utilization.

## deliverable-doc

- `t_135f9214` — "deliverable: Google Doc — exact PUT request + shape audit"
  Summary: "Shape audit Doc created and shared. URL: https://docs.google.com/..."
  **DOC-1**: check doc for unrendered markdown.
  **DOC-2**: check sharing.

## exploration

- `t_65d333af` — "migrate last-24h LangSmith traces"
  This is an ops task, likely `other` not exploration.

## other

- `t_0bc7806c` — "sync(fork): rebase sahilm-ti/hermes-agent main onto NousResearch upstream"
  No PR, no investigation, no skill-edit. Class: `other`.
  **OTH-1**: summary must be > 100 chars.

- `t_fbcd79bd` — "address Sahil's inline comments on PostHog hive shape-audit Doc"
  Class: `other` (follow-up doc edits).
  **OTH-1**: summary check.
