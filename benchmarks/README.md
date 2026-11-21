# Benchmark results

Claim judgments were made by Codex and Claude subagents. Each was given only its manifest or packet, with no labels or web access. Reference matching, retrieval, source hashing, quote validation, and scoring ran in Python. Bibliographic identity, claim judgment, and provenance have different denominators.

## HALLMARK reference matching

Four disjoint, hash-selected samples from the repository's pinned 831-row HALLMARK split. Each sample has 30 corrupted and 16 valid entries. Gold labels are joined only after all match decisions are recorded.

The current matcher adds three lookups to Crossref and OpenAlex. Semantic Scholar supplies conference venues and years that preprint records lack. A doi.org registration check confirms a DOI that no index knows. Records that share a DOI, an arXiv ID, or a title and first author are grouped as one work. The initial and holdout samples were used to develop these changes. The confirmation sample was used for the earlier release's tradeoff check. The final sample was not examined by any version before scoring.

Strict F1 counts a metadata discrepancy or a completed index miss as a flag.

| Sample | Current matcher | Earlier release | Historical auditor | Crossref DOI-only |
|---|---:|---:|---:|---:|
| Initial (development) | 0.984 | 0.846 | 0.873 | 0.171 |
| Holdout (development) | 0.966 | 0.776 | 0.873 | 0.429 |
| Confirmation | 0.931 | 0.769 | 0.881 | 0.229 |
| Final (unseen) | **0.983** | 0.903 | 0.900 | 0.400 |

On the final sample the current matcher flagged 29 of 30 corruptions with no false flag. The earlier release flagged 28 with 4 false flags. Triage sends every unverified result for review. It catches all 30 corruptions in every sample and now sends 2 of 16 valid entries to review instead of 6 to 9.

Development fixed five failure patterns:

- A cited year in the future was ignored when only a preprint record was found.
- A DOI unknown to every index was left unconfirmed. doi.org now shows whether it was registered.
- A conference year far from the preprint year was excused. A conference year more than one year after the first version is now flagged when Semantic Scholar identifies the venue as a conference. Journal versions stay open because they can take several years.
- An invented title close to a real one was left ambiguous. Without a shared author it is now not found. A single near-identical title with the same first author is now the cited work with a title mismatch.
- A conference venue such as ICML or ICLR could not be checked from preprint records.

Remaining errors on the confirmation and final samples:

- Three invented titles share an author with a real candidate, so they stay ambiguous.
- One invented workshop venue has only a preprint record and stays unconfirmed.
- One valid JMLR entry is flagged because the index year is two years earlier than the cited year.
- In development, one valid JMLR paper was flagged because Semantic Scholar merged it with an AISTATS version.

Semantic Scholar answered 45 or 46 of 46 lookups per sample after retries. During the live final run, OpenAlex's keyless daily budget ran out, and its lookups failed for 14 of 30 rows. Both matchers were scored on those same captured responses. Keyless coverage varies by day, so the CLI accepts `OPENALEX_API_KEY` and `SEMANTIC_SCHOLAR_API_KEY`.

Run from the Research Paper Auditor root. The script checks the fixture digest. `--offline` replays a captured cache. A fresh live run can differ as indexes change.

```sh
python -B cite-checker/benchmarks/hallmark.py --split final --historical runs/hallmark/results_fixed.jsonl --cache <cache-dir>
```

See `hallmark-v2-*.json` for the current matcher and `hallmark-release-*.json` for the earlier release, including row statuses and every source attempt. [HALLMARK](https://github.com/rpatrik96/hallmark)

## End-to-end manuscript audit

`fixtures/end-to-end` holds a 15-sentence manuscript and its BibTeX file. It cites seven real arXiv papers and one invented work, and one real entry gives the wrong venue. Its 15 claims include exact results, altered numbers, a figure taken from the wrong table row, facts the paper never addresses, and claims citing the wrong paper. `prepare` resolved every reference live and retrieved all seven papers' full text from arXiv PDFs.

| Check | Result |
|---|---:|
| References classified correctly | 8 of 8 |
| Claims judged correctly, Claude run A | 15 of 15 |
| Claims judged correctly, Claude run B | 15 of 15 |
| Quote validation failures | 0 |

The wrong venue was caught because Semantic Scholar records the paper at ICLR. The claims were written to have clear answers. This shows the full pipeline working, and it is not a measure of judgment on hard cases.

```sh
python cite-checker/scripts/cite_checker.py prepare cite-checker/benchmarks/fixtures/end-to-end/paper.md --bib cite-checker/benchmarks/fixtures/end-to-end/refs.bib --out <run-dir>
python cite-checker/benchmarks/end_to_end.py <run-dir>/report.json
```

See [the recorded statuses and scores](end-to-end-results.json).

## SciFact abstract judgments

The official dev archive gives 323 eligible claim–source pairs. Each condition saw the same supplied abstracts in closed-book conditions. The three-way scorer counts `partially_supported` as a miss because SciFact has no partial class.

### Claude

A fresh 48-pair sample, 16 per label, shares no claim or cited document with any earlier sample. `scifact.py prepare` writes it as `fresh.json`. Each condition ran twice with independent Claude subagents.

| Condition | Run 1 | Run 2 |
|---|---:|---:|
| Unguided | 38/48 | 38/48 |
| Skill as released | 37/48 | 39/48 |
| Skill with rewritten verdict rules | 39/48 | 38/48 |

All conditions fell within one item of each other, so the skill's guidance neither helps nor hurts Claude's verdicts here. The rewritten rules scored 33/36 on the confirmation sample against 30/36 unguided. Those rules were written after studying that sample's errors, so that gain does not count, and the rules were not adopted.

Every run missed the same seven items. In three of them, the abstract contradicts the gold label:

- item-033 is labeled supported for a 10% decline in heart failure incidence, but the abstract says incidence "did not change over time among men or women."
- item-004 is labeled insufficient, but the abstract reports the reverse of the claim.
- item-044 is labeled contradicted, but the abstract supports the claim.

With label noise of this size, this closed-book sample can no longer separate the conditions. Every guided run passed the quote validator with no failures. See [the Claude scoring record](scifact-claude-results.json).

### Codex

| Sample | With skill | Unguided Codex |
|---|---:|---:|
| Development | 14/18 | 14/18 |
| Holdout | 18/24 | 17/24 |
| Confirmation | 19/36 | 24/36 |

The confirmation gap was specific to Codex. The released wording on nonsignificant results and on contradiction under matching conditions led Codex to call several null-result contradictions insufficient. An example is "macrolides protect against myocardial infarction" cited to a study finding no protective effect. Claude did not show this pattern with the same wording. These were single runs per condition. See [the Codex confirmation record](scifact-confirmation-results.json).

### Evidence packets

Abstracts up to 8,000 characters now appear intact in the packet instead of as overlapping top-five passages. Every SciFact abstract is under that limit, so the packet always contains each complete rationale by construction. Removing the overlap also cuts packet text by 11.8%. Neither figure measures retrieval quality.

The passage ranker alone retrieved all sentences of at least one gold rationale for 130 of 209 annotated pairs at rank 1, 202 at rank 3, and 206 at rank 5. It is still used for full texts and long abstracts.

Download the [official SciFact data archive](https://scifact.s3-us-west-2.amazonaws.com/release/latest/data.tar.gz). The script checks its SHA-256 before reading. Give only the selected manifest to each evaluator and keep the gold files for scoring.

```sh
python -B cite-checker/benchmarks/scifact.py prepare data.tar.gz <evaluation-dir>
python -B cite-checker/benchmarks/scifact.py score <evaluation-dir>/gold/fresh.json <run-dir>/report.json
```

[SciFact dataset and rationale definitions](https://github.com/allenai/scifact/blob/master/doc/data.md)

## Version 2: parallel lookups, triage labels and cheap-model shards

These runs used the plugin layout in this repository (`skills/cite-checker/scripts`).

### HALLMARK, live rerun

All four samples were rerun live on 2026-11-21 against the pinned fixture (HALLMARK commit `908a405`, SHA-256 `e42ef2cb…`). True and false positive counts match the version 1 matcher on every sample, so the DataCite route and the thread-safe transport changed no decision.

| Sample | Strict F1 | Version 1 |
|---|---:|---:|
| Initial | 0.984 | 0.984 |
| Holdout | 0.966 | 0.966 |
| Confirmation | 0.931 | 0.931 |
| Final | 0.983 | 0.983 |

The triage label comes from `classify()`, with no model. Across the 184 rows:

| HALLMARK type | Triage labels |
|---|---|
| swapped_authors | chimeric 16, metadata_error 2 |
| merged_citation | chimeric 7 |
| hybrid_fabrication | chimeric 3 |
| placeholder_authors | chimeric 7 |
| fabricated_doi | chimeric 11 |
| plausible_fabrication | phantom 16, needs_review 3 |
| chimeric_title | phantom 3 |
| future_date, wrong_venue, preprint_as_published, partial_author_list | metadata_error 20 |
| near_miss_title | metadata_error 8, needs_review 1 |
| nonexistent_venue | metadata_error 9, unconfirmed 2 |
| arxiv_version_mismatch | metadata_error 11, chimeric 1 |
| valid (64) | verified 57, unconfirmed 5, metadata_error 2 |

No valid entry was labelled phantom or chimeric. `chimeric_title` entries invent a title around real authors, and no index holds that title, so they fall under phantom. Results: `hallmark-v3-*.json`.

### SciFact through shards

Each run used `prepare --local-only`, then `shard` (8 pairs per shard, top 3 passages), then `judge --runner "claude -p --model haiku"`, then `finalize`. The pairs that `finalize` escalated (failed quotes, `contradicted` and `partially_supported` calls) were re-judged with `claude -p --model sonnet`. The fresh sample was used to tighten the `partially_supported` rule in `references/judging.md`. The confirmation sample was not used for that change.

| Sample | Haiku only | After Sonnet escalation | Best version 1 run | Escalated pairs |
|---|---:|---:|---:|---:|
| Confirmation (36) | 0.908 (30/36) | 0.925 (31/36) | 0.916 (33/36) | 17 |
| Fresh (48) | 0.742 (34/48) | 0.727 (33/48) | 0.855 (39/48) | 18 |

Macro F1, with correct counts in brackets. No quote failed validation. On the confirmation sample the first pass sent about 18,600 input tokens to Haiku and the escalation sent about 9,900 to Sonnet, estimated at four characters per token from `meter-*.json`. On the harder fresh sample the cheap pipeline trails the version 1 runs, where a stronger model read the whole packet; escalation did not recover it. Of the 15 fresh-sample errors, 5 are supported claims both models called `partially_supported` (SciFact has no partial label) and 4 are `insufficient` calls, which are not escalated. Results: `scifact-v3-shards-results.json`.

## Version 2.1: batched lookups, quorum rule, Opus escalation

### Speed

| Step | Version 1 | Version 2.1 |
|---|---:|---:|
| HALLMARK sample, 46 references, live | 172-233 s | 22-74 s |
| End-to-end fixture `prepare`, empty cache | 38 s | 14-20 s |
| End-to-end fixture `prepare`, warm shared cache | 38 s (no shared cache) | 5 s |

Semantic Scholar records for every reference with a DOI or arXiv ID now come from one batch request, and arXiv records from one `id_list` request. On the end-to-end fixture that cut Semantic Scholar calls from 8 to 2 and arXiv API calls from 7 to 1. The remaining time is mostly downloading full texts from arxiv.org. Part of the HALLMARK speed-up on `fresh1` to `fresh4` comes from OpenAlex answering HTTP 429 at once, because its keyless daily budget was spent during these runs.

### HALLMARK

Eight disjoint samples of 46 rows (30 corrupted, 16 valid). `fresh1` and `fresh2` were scored first and then used to fix three rules: author lists cut to a first and last author were missed, name variants such as "Kui Ren" and "Kui Rent" were flagged, and one valid paper became phantom while OpenAlex and Semantic Scholar were rate-limited. `fresh3` and `fresh4` were scored once, after every change. Scores below replay the captured responses with the final code (`--indices crossref openalex semanticscholar arxiv`).

| Sample | Strict F1 | Errors | Version 1 strict F1 |
|---|---:|---|---:|
| Initial | 0.984 | 1 valid flagged (year, venue) | 0.984 |
| Holdout | 0.983 | 1 invented venue unconfirmed | 0.966 |
| Confirmation | 0.984 | 1 valid flagged (year) | 0.931 |
| Final | 0.983 | 1 invented venue unconfirmed | 0.983 |
| fresh1 | 0.983 | 1 hybrid fabrication ambiguous | 0.949 (first scoring) |
| fresh2 | 0.966 | 2 near-miss titles that differ from the real title only by a hyphen | 0.842 (first scoring) |
| fresh3 (unseen) | **1.000** | none | |
| fresh4 (unseen) | **0.951** | 1 wrong venue unconfirmed, 2 valid flagged (authors, year) | |

Across all 368 rows the strict label is right for 358 (97.3%). OpenAlex returned HTTP 429 for 22 to 30 rows of each fresh sample, so those rows were judged on Crossref, Semantic Scholar and arXiv only. "Not found" now needs two indexes to have answered, so an outage gives `lookup_unavailable` and not a false phantom. Of the 128 valid rows, 118 were verified, 4 unconfirmed, 2 sent to review and 4 flagged as metadata errors. None was labelled phantom or chimeric. Results: `hallmark-2.1-*.json`.

The remaining misses need data the free indexes do not hold: a register of real venue names (DBLP blocks scripted access), and title differences of one hyphen, which BibTeX entries also drop from valid titles.

### SciFact

The judging rules changed in two ways. A scope gap (another species, cohort or condition) goes into a `caveat` field and no longer turns `supported` into `partially_supported`. A source stating the opposite of the main finding is `contradicted`. Escalation now also covers `insufficient` calls on claims that state a number, direction or negation, and escalated pairs go to Opus. Errors from the version 2 fresh and confirmation runs were read while writing these rules; dev and holdout were not.

| Sample | Haiku | Haiku, then Opus on escalated pairs | Escalated | Version 2 (Haiku, then Sonnet) |
|---|---:|---:|---:|---:|
| Dev (18) | 0.836 (15/18) | 0.836 (15/18) | 9 | |
| Holdout (24) | 0.782 (18/24) | 0.872 (21/24) | 17 | |
| Confirmation (36) | 0.957 (33/36) | 0.958 (34/36) | 18 | 0.925 |
| Fresh (48) | 0.795 (37/48) | 0.875 (42/48) | 29 | 0.727 |

Macro F1, with correct counts in brackets: 112 of 126 overall (89%). No quote failed validation. Opus read about 43,000 input tokens across the four samples, and Haiku about 69,000.

About half of the 14 remaining errors are cases where the abstract appears to settle the claim differently from the SciFact label. The source says IFN-gamma-deficient mice "developed fatal autoimmune myocarditis" for a claim that they are resistant (label: not enough information). It says Scribble loss promotes tumorigenesis for a claim that it prevents it (label: not enough information). It says heart failure incidence did not change for a claim of a 10% fall (label: supported). The others are inferences the models declined to make, such as reading a non-comparative abstract as contradicting a comparative claim. Results: `scifact-2.1-shards-results.json`.

## Closed-book stress test

A nine-claim fixture covers ten claim–source pairs. It includes a reversed effect, an incompatible denominator, a missing source, an uncollected endpoint, a nonsignificant result cited as equivalence, two sources on one claim, and an instruction embedded in source text. Codex audited all ten pairs. Separate deterministic tests reject wrong-source quotes, altered text, stitched quotes, wrong offsets, changed digests, and stale assessments.

## Related tools

- [bibtex-updater](https://github.com/rpatrik96/bibtexupdater/blob/main/benchmarks/HALLMARK.md) and [citecheck](https://arxiv.org/abs/2603.17339) focus on bibliographic verification. Both are useful comparators for a future matched metadata trial.
- [check-references](https://github.com/angelgalvisc/check-references) and [citation-integrity-auditor](https://github.com/UCL-ERL/skills/blob/main/skills/writing/citation-integrity-auditor/SKILL.md) cover source-level support.
- [Tuto](https://github.com/tao-hpu/tuto) implements academic citation resolution and claim checking with its own pipeline.

This package combines a portable install, keyless scholarly lookup, the host agent's semantic judgment, and a quote gate that replays independently. These related tools were reviewed for features only.
