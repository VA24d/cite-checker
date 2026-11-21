# Changelog

## 2.2.0

- New `suggest` command finds the right citation for claims no cited source supports. It ranks every other captured source against the claim, so a better-matching reference already in the bibliography is reported as a likely wrong or swapped citation. With `--external` it also searches OpenAlex and Semantic Scholar with claim keywords. Candidates go to the same judges and quote gate, and the ones judged to support the claim appear under "Suggested citations" in the report.
- The `/cite-check` command and the skill run `suggest` after escalation.
- Licensed under PolyForm Strict 1.0.0 with an attribution requirement: noncommercial use only, no redistribution or derivative works, and credit to cite-checker in any work that uses it. `CITATION.cff` gives the citation, and reports end with a credit line.

## 2.1.0

New checks done in code, where language models are unreliable:

- LaTeX `\input` and `\include` files are inlined and macros defined in the manuscript are expanded, including macros with arguments. Section titles, labels and inline formatting no longer leak into claims.
- Clause-level citation mapping: when a sentence reports the authors' own result and cites prior work for its last clause, only that clause is checked. First-person claims ("We use X [1], a dataset of ...") are judged on what they say about the cited work.
- Number checks: claim numbers missing from the source are recomputed from source numbers (percentages, ratios, differences, percent change), and differing source values for the same quantity go to the judge as hints. Abstract and conclusion numbers that the body never states, or states differently, are reported.
- Package registry checks: packages and versions named in claims are checked against npm, PyPI, crates.io and GitHub (`--no-registries` turns this off).
- Date checks: claims about a year after the cited work appeared, and access dates in the future or before publication.
- Secondary citations: a supported claim whose quoted passage credits other work is flagged so the original can be cited.
- Crossref corrections, errata, expressions of concern and retractions, and newer arXiv versions of a pinned citation, appear as advisories.

Sources:

- New lookup indexes: OpenReview and PubMed (E-utilities; `NCBI_API_KEY` optional).
- Full text from open-access PDFs listed by OpenAlex, from Unpaywall when `--mailto` is given, and from CORE when `CORE_API_KEY` is set.
- Semantic Scholar batch lookups, a two-index quorum so an index outage gives `lookup_unavailable` instead of `not_found`, and an arXiv title-search fallback.

Judging:

- Structured judging mode (`judge --structured`) and majority voting (`--votes N`).
- Pairs already answered in `escalate-*.json` are not escalated again.
- Corporate and team bylines on web pages no longer produce `chimeric` labels.

Packaging:

- CI on Linux and macOS for Python 3.10, 3.12 and 3.13.
- Tag-triggered releases that attach the skill archive and the plugin archive.

Also: cited GitHub pull requests now include their changed lines as source text, and arXiv HTML full text is found by page title (it was falling back to the PDF).

## 2.0.0

- Packaged as a Claude Code plugin with a `/cite-check` command and a Haiku `claim-judge` agent.
- Parallel lookups, triage labels, web-page and CITATION.cff checks, bibliography structure checks, and cheap-model shards with an Opus escalation pass.
