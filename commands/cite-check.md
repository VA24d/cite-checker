---
description: Audit a paper's references and cited claims with cite-checker
argument-hint: <paper.pdf|.tex|.md|.bib|manifest.json> [--bib refs.bib] [--references-only]
---

Use the cite-checker skill to audit: $ARGUMENTS

Follow the skill's steps in order: prepare, shard, judge the shards with parallel `claim-judge` agents, finalize, escalate only what the finalize summary lists, then run `suggest` for claims no citation supports (ask before using `--external`), judge and finalize again, then report.
