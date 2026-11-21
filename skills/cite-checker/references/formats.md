# Input and output

`prepare` accepts `.bib`, `.json`, `.md`, `.txt`, `.tex`, or `.pdf`. Use `--bib PATH` more than once to add bibliographies. The lightweight text parser recognizes bracketed numbers and ranges, Pandoc citation keys, and common LaTeX citation commands. PDF requires `pdftotext` and a readable text layer. Multicolumn PDFs, author–year styles, scanned pages, and multi-file manuscripts can need manual mapping. The auditor's `ParsedPaper` JSON preserves its existing bibliography and citation-to-sentence links.

Use a manifest for hand-checked links, quotes supplied in chat, or an input style the parser missed. Reference fields are optional except `id`. Use each ID once. Source `text` or `path` is optional. A path is relative to the manifest, or absolute. A source file may be text or PDF. Set `level` to `abstract`, `excerpt`, or `full_text` when known. The default is `local`. Use `url` for an attribution link when available.

```json
{
  "references": [
    {
      "id": "trial",
      "title": "Trial title",
      "doi": "10.1234/example",
      "level": "excerpt",
      "text": "The reduction at week 8 was 13%."
    }
  ],
  "claims": [
    {
      "id": "c1",
      "text": "The reduction at week 8 was 30%.",
      "references": ["trial"]
    }
  ]
}
```

Other reference fields include `authors` (array), `authors_complete` (set to true for a full author list), `year`, `venue`, `arxiv_id`, and `raw`. A draft may cite multiple works per claim. Each listed source gets its own assessment.

`packet.json` records reference lookup results, source provenance, and the evidence presented to the agent. Abstracts up to 8,000 characters appear intact. Longer texts use ranked passages. `sources.json` contains the frozen text used by the quote validator. `finalize` runs locally with these two files and the assessments array. It emits `report.md` and `report.json`. Keep the packet and assessments to reproduce the report. The HTTP cache is useful for rerunning preparation against captured responses.

An assessment row has `pair_id`, `verdict`, `reason`, and `quotes`. Copy the complete pair ID from the packet. It binds the claim and its source digest, so changed claims or sources need fresh assessments. Quotes can be strings. For repeated text, use an object with `text` and the zero-based `start` character offset in the source. Optionally include `reference_id` and `sha256`. JSON evidence contains exact source slices and end-exclusive offsets. Copy each disjoint passage as a separate quote. Omit quotes when no relevant source text exists. A missing judgment on a readable pair is reported as `not_assessed`.

## Source recovery

Crossref and OpenAlex handle metadata. Semantic Scholar supplies conference venues and years that preprint records lack, such as ICML, ICLR, and NeurIPS. When a stated DOI is missing from every index, the CLI asks doi.org whether the DOI is registered. The CLI also checks arXiv identifiers and Europe PMC DOI records, and retrieves arXiv HTML, arXiv PDFs with `pdftotext`, and Europe PMC full-text XML when available. It can fall back to indexed abstracts.

An index may rate-limit anonymous requests. Semantic Scholar's keyless pool often returns HTTP 429, and OpenAlex caps keyless use with a daily budget shared per IP address. Set `SEMANTIC_SCHOLAR_API_KEY` or `OPENALEX_API_KEY` to use a free key. Keys are sent only with live requests and never stored in the cache. Without its record, a venue given only by a preprint stays `metadata_unconfirmed` rather than mismatched. Check the recorded HTTP attempts and the open-access links in the packet before declaring a lookup unresolved.

For other publishers, use the host's browser or fetch tools on the identified citation link. Confirm the title, authors, and identifier or version on the source itself. Capture the original text as a local file or exact excerpt, and add it to the matching reference in the manifest. Keep the original URL and accurately set the source level. Rerun `prepare` in a fresh directory, reusing the lookup cache if appropriate. Search by public bibliographic metadata. Leave inaccessible or mismatched sources unresolved.
