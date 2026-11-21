---
name: cite-checker
description: Use when the user asks to verify academic citations, DOI or arXiv references, BibTeX bibliographies, cited web pages or software, or whether cited research supports manuscript claims.
---

# Cite checker

Reference checks are deterministic Python. Only claim support needs a model, and that work goes to small batches that a cheap model can judge. Run everything from the user's workspace with Python 3.10 or later. `<skill>` is this skill's directory.

## 1. Prepare (no model tokens)

```sh
python "<skill>/scripts/cite_checker.py" prepare "<input>" --out "<new-run-dir>"
```

Options: `--bib FILE` (repeatable), `--references-only`, `--local-only` (closed book, supplied text only), `--offline --cache PATH` (replay), `--no-web` (skip cited web pages), `--workers N` (parallel lookups, default 8), `--cache DIR` and `--cache-days N` (shared HTTP cache, default `~/.cache/cite-checker`, refreshed after 30 days), `--mailto EMAIL` (contact address for Crossref and Unpaywall; Unpaywall full text runs only with it; ask the user before using any address), `--no-registries` (skip npm, PyPI, crates.io and GitHub checks). Optional keys in the environment: `NCBI_API_KEY` (faster PubMed), `CORE_API_KEY` (CORE full text). If the parser misses claims or the user gives claims and sources in chat, write a manifest as in [formats](references/formats.md).

The command prints a summary. Report from it and from `report.md` later. Do not open `packet.json` or `sources.json` into context: they are large and the scripts read them for you.

- `triage` labels each reference without judgment: `phantom` (no index holds it), `chimeric` (real work spliced with another's authors or identifier), `metadata_error`, `dead_link`, `needs_review`, `unconfirmed`, `unresolved`, `verified`.
- `structure_issues` counts bibliography problems: undefined or uncited keys, duplicates, missing fields, missing access dates, one venue spelled several ways, numbered-style order.
- `manuscript_checks` counts checks of the manuscript's own statements: a claim about a year after the cited work appeared, access dates in the future or before publication, abstract numbers missing from the body or stated two ways, and packages or versions named in claims that the registry contradicts (`pako@9.9.9` does not exist; a package said not to exist does).
- LaTeX `\input` files and macros defined in the manuscript are expanded before claims are read. When a sentence states the authors' own result and cites prior work for its last clause, the claim is that clause.
- Lookups use Crossref, OpenAlex, Semantic Scholar, arXiv, DataCite, Europe PMC, OpenReview and PubMed. Full text comes from arXiv, Europe PMC, open-access PDFs listed by OpenAlex, Unpaywall and CORE before falling back to abstracts.
- Advisories in the report give the citation the authors ask for: published version of a cited preprint, CITATION.cff, a DOI declared on a page, "please cite" text, or an archived copy of a dead link.

## 2. Judge claims with cheap models

Skip this for a bibliography-only run.

```sh
python "<skill>/scripts/cite_checker.py" shard "<run-dir>"
```

This writes `<run-dir>/shards/shard-NN.json`, about 8 claim-source pairs each, holding only claims, short citations, top passages and number hints. Pairs without source text are left out; they are already `insufficient`.

- **Claude Code**: launch one `claim-judge` agent (Haiku) per shard, all in parallel, in one message. Give each the shard path, the rules path `<skill>/references/judging.md`, the run dir and the output path `<run-dir>/assessments/shard-NN.json`.
- **Any other CLI** (Gemini Flash, `claude -p`, Codex): `python "<skill>/scripts/cite_checker.py" judge "<run-dir>" --runner "<cli and flags that take a prompt as last argument>"`. Example runner: `agy --sandbox --mode plan --model gemini-3.8-flash-high --dangerously-skip-permissions -p`.
- **No subagents**: judge the shards yourself, following `references/judging.md`.

Never send manuscript claims to an external model or search service without the user's permission.

## 3. Finalize and escalate

```sh
python "<skill>/scripts/cite_checker.py" finalize "<run-dir>" --assessments "<run-dir>/assessments"
```

Quotes are validated against the frozen source text. A decisive verdict without a verbatim quote becomes `insufficient`. If the summary has `escalate`, it lists `escalate-NN.json` shards: failed quotes, every `contradicted` or `partially_supported` call, and `insufficient` calls on claims with a number, direction or negation. Re-judge only those with the strongest model available (yourself, or `judge --pattern "escalate-*.json" --runner "claude -p --model opus"`), save answers as `<run-dir>/assessments/escalate-NN.json`, and finalize again. Escalated answers replace first-pass ones, and pairs already answered in `escalate-*.json` are not escalated again. A supported claim whose quoted passage itself credits other work ("[12]", "Smith et al. (2019)", "prior work found") is marked as a secondary citation.

## 4. Report

Link `report.md` and `report.json`. Lead with phantom and chimeric references, then claims that are contradicted or need narrowing, then structure issues and preferred-citation advisories. Name coverage gaps (unresolved references, pairs without sources). Use only verified records for suggested corrections. Keep the manuscript unchanged unless the user asked for edits. For sources the scripts could not fetch, see [source recovery](references/formats.md#source-recovery).
