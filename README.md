# Cite checker

An installable Agent Skill for checking academic references and the claims made about them. It adapts the Research Paper Auditor's resolution, passage-ranking, and evidence-gating workflow. The scripts use Python's standard library and public scholarly indexes. The agent you already use interprets the cited text.

## Install as a Claude Code plugin

In Claude Code:

```text
/plugin marketplace add VA24d/cite-checker
/plugin install cite-checker@cite-checker
```

For a local clone, pass its folder path instead. The plugin adds the `cite-checker` skill, a `/cite-check` command and a `claim-judge` agent that runs on Haiku.

For Claude.ai or any host that takes a skill archive, run `python package.py` and upload `dist/cite-checker.zip`.

## Install for other hosts

Run from the folder containing this README with Python 3.10 or later:

```sh
python install.py
```

This installs the skill in your user folders. Add `--hosts codex claude gemini t3` with just the hosts you want. The `t3` target installs the Codex skill. Include `claude` for T3 sessions using Claude. Add `--project PATH` for a project-only installation. The installer checks for conflicts and supports repeat installs of identical files.

| Host | User skill location | Invocation |
|---|---|---|
| Codex | `~/.agents/skills/cite-checker` | `$cite-checker Audit paper.pdf and refs.bib` |
| Claude Code | `~/.claude/skills/cite-checker` | `/cite-checker Audit paper.pdf and refs.bib` |
| Gemini CLI | `~/.gemini/skills/cite-checker` | `/cite-checker Audit paper.pdf and refs.bib` after installing the command wrapper, or ask Gemini to use the cite-checker skill |
| T3 Code | The selected provider's skill location on the machine running the provider | With Codex, select `$cite-checker` in the composer or ask it to use the named skill. With Claude, use its installed skill. |

Start a fresh session after installation. Install on the linked host if T3 Code runs the provider on another machine. For PDF, install a text extractor that provides the `pdftotext` command, such as Poppler. You can also provide extracted text, BibTeX, or a structured manifest.

[Codex skill locations](https://developers.openai.com/api/reference/cli) · [Claude Code skills](https://code.claude.com/docs/en/skills) · [Gemini CLI skills](https://geminicli.com/docs/cli/skills/) · [Gemini CLI custom commands](https://geminicli.com/docs/cli/custom-commands/) · [T3 Code's Codex skill discovery](https://github.com/pingdotgg/t3code/blob/main/apps/server/src/provider/CodexProvider.ts)

## Use

- **Bibliography check** — “Audit `references.bib` for wrong DOI, title, authors, year, or venue.”
- **Claim check** — “Check whether each cited source supports the claims in `paper.pdf`. Report the wording that needs revision.”
- **Closed-book check** — “Check this draft against these supplied excerpts only. Use local-only mode.”

The report separates scholarly record matches and discrepancies from claim-support judgments. Exact quotes are validated against a frozen source, with a SHA-256 digest and character offsets in the JSON output. Reference lookup uses Crossref, OpenAlex, arXiv, Europe PMC, and Semantic Scholar without keys. Semantic Scholar supplies conference venues such as ICML and NeurIPS. Keyless pools are rate-limited, and OpenAlex now caps keyless use with a daily budget shared per IP address. Set `OPENALEX_API_KEY` or `SEMANTIC_SCHOLAR_API_KEY` if you have a free key. Live indexes can change or rate-limit anonymous access. Finalization replays locally with the captured source text and assessments. Use the closed-book mode for material you want to keep entirely local.

Reference checks run in Python with no model. Each reference gets a triage label: `phantom` when no index holds it, `chimeric` when a real work is cited with another work's authors or identifier, `metadata_error` for smaller title, year, venue or author errors, `dead_link`, `needs_review`, `unconfirmed`, `unresolved` or `verified`. URL-only entries are checked against the cited page's `citation_*`, Open Graph and JSON-LD metadata, with a Wayback Machine copy for dead links. DataCite covers software and dataset DOIs. The report lists the citation the authors ask for: a published version of a cited preprint, a GitHub `CITATION.cff`, a DOI declared on the page, or "please cite" text. It also lists bibliography structure problems, such as undefined or uncited keys, duplicates, missing fields or access dates, and one venue spelled several ways.

The CLI can also run independently:

```sh
S=skills/cite-checker/scripts/cite_checker.py
python $S prepare paper.tex --bib refs.bib --out audit     # lookups, 8 in parallel
python $S shard audit                                      # ~8 pairs per compact batch
python $S judge audit --runner "claude -p --model haiku"   # or any CLI taking a prompt
python $S finalize audit --assessments audit/assessments   # quote gate, report, escalations
```

`finalize` writes `escalate-NN.json` shards for failed quotes and for every contradicted or partially supported call. Judge those with a stronger model into `audit/assessments/escalate-NN.json` and finalize again; those answers replace the first pass. See the [manifest and assessment formats](references/formats.md) and the [benchmark results](benchmarks/README.md).

## Repository layout

| Path | Contents |
|---|---|
| `.claude-plugin/` | Plugin manifest and a one-plugin marketplace |
| `skills/cite-checker/` | The skill: `SKILL.md`, the standard-library scripts, and the judging and format references |
| `agents/claim-judge.md` | Haiku agent that judges one shard |
| `commands/cite-check.md` | `/cite-check` slash command |
| `benchmarks/` | HALLMARK, SciFact and end-to-end benchmarks with recorded results |
| `tests/` | Unit tests; run `python -m pytest` |
| `install.py`, `package.py` | Installer for Codex, Gemini CLI and T3; skill archive builder |
