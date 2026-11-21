"""Read the manuscript and keep the link between each claim and its cited work."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote

DOI = re.compile(r"10\.\d{4,9}/[^\s<>{}\"\\]+", re.I)
ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]+/\d{7})(?:v\d+)?"
ARXIV = re.compile(r"(?:arxiv:\s*|arxiv\.org/(?:abs|pdf|html)/)(" + ARXIV_ID + r")", re.I)
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
URL = re.compile(r"https?://[^\s{}<>\"\\]+")
NUMERIC = re.compile(r"\[(\d+(?:\s*(?:[-\u2013\u2014,;]\s*\d+))*)\]")
PANDOC = re.compile(r"(?<![\w@])@([\w:./-]+)")
LATEX_CITE = re.compile(
    r"\\(?:[A-Za-z]*cite[A-Za-z]*|cite[A-Za-z]*)\*?(?:\s*\[[^\]]*\]){0,2}\s*\{([^{}]+)\}", re.I
)
REF_HEADING = re.compile(
    r"(?im)^[ \t\f]*(?:#{1,6}[ \t]+)?(?:references|bibliography)[ \t]*:?[ \t]*$"
)
REF_ITEM = re.compile(r"(?m)^[ \t\f]*(?:\[(\d+)\]|(\d{1,4})[.)])[ \t]+")
REF_STOP = re.compile(r"(?im)^[ \t\f]*(?:#{1,6}[ \t]+|(?:appendix|supplementary material)\b)")
TEX_BIB = re.compile(
    r"\\begin\{thebibliography\}(?:\s*\{[^{}]*\})?(.*?)\\end\{thebibliography\}", re.S
)
BIB_ITEM = re.compile(r"\\bibitem(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}")
MONTHS = dict(
    zip(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        strict=True,
    )
)


def doi_of(value):
    match = DOI.search(unquote(str(value or "")))
    if match is None:
        return ""
    doi = match[0].rstrip(".,;:").casefold()
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    return doi.rstrip("]")


def arxiv_of(value, bare=False):
    value = str(value or "").strip()
    match = ARXIV.search(value)
    if match:
        return match[1].lower()
    return value.lower() if bare and re.fullmatch(ARXIV_ID, value, re.I) else ""


def reference(row):
    raw = str(row.get("raw") or "").strip()
    authors = row.get("authors", row.get("author", [])) or []
    if isinstance(authors, str):
        authors = re.split(r"\s+and\s+", authors)
    link = str(row.get("url") or "")
    if not link:
        # @misc and @online entries often put the link in howpublished or note.
        for field in ("howpublished", "note"):
            if found := URL.search(str(row.get(field) or "")):
                link = found[0].rstrip(".,;")
                break
    doi = doi_of(row.get("doi")) or doi_of(link) or doi_of(raw)
    arxiv = arxiv_of(row.get("arxiv_id") or row.get("eprint"), bare=True)
    if not arxiv:
        arxiv = arxiv_of(link or raw)
    if not arxiv and doi.startswith("10.48550/arxiv."):
        arxiv = arxiv_of(doi.split("/arxiv.", 1)[1], bare=True)
    year = YEAR.search(str(row.get("year") or ""))
    return {
        "id": str(row["id"]),
        "title": str(row.get("title") or "").strip(),
        "authors": [str(a).strip() for a in authors if str(a).strip()],
        "authors_complete": bool(row.get("authors_complete", "author" in row)),
        "year": int(year[0]) if year else None,
        "venue": str(row.get("venue") or row.get("journal") or row.get("booktitle") or "").strip(),
        "doi": doi,
        "arxiv_id": arxiv,
        "url": link,
        "raw": raw,
        **({"entry_type": str(row["entry_type"])} if row.get("entry_type") else {}),
        **({"accessed": str(row["urldate"])} if row.get("urldate") else {}),
        **{k: row[k] for k in ("path", "text", "level") if k in row},
    }


def _close(text, start, opener, closer):
    """Index of the closing delimiter, with nested braced literals preserved."""
    depth = 0
    in_quotes = False
    braces = 0
    i = start
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if opener == closer:
            if char == "{" and i > start:
                braces += 1
            elif char == "}" and braces:
                braces -= 1
            elif char == closer and i > start and not braces:
                return i
        elif opener == "{":
            depth += (char == "{") - (char == "}")
            if not depth:
                return i
        else:
            braces += (char == "{") - (char == "}")
            if char == '"' and not braces:
                in_quotes = not in_quotes
            if not braces and not in_quotes:
                depth += (char == opener) - (char == closer)
                if not depth:
                    return i
        i += 1
    raise ValueError("unclosed BibTeX entry or field")


def _fields(payload, macros):
    i = 0
    fields = {}
    while i < len(payload):
        key = re.match(r"[\s,]*([\w-]+)\s*=\s*", payload[i:])
        if not key:
            if payload[i:].strip(" \n\r\t,"):
                raise ValueError("could not parse BibTeX field near " + payload[i : i + 60])
            break
        i += key.end()
        values = []
        while i < len(payload):
            char = payload[i]
            if char in '{"':
                stop = _close(payload, i, char, "}" if char == "{" else '"')
                values.append(payload[i + 1 : stop])
                i = stop + 1
            else:
                term = re.match(r"[^,\s#]+", payload[i:])
                if not term:
                    raise ValueError("empty BibTeX field " + key[1])
                values.append(macros.get(term[0].lower(), term[0]))
                i += term.end()
            while i < len(payload) and payload[i].isspace():
                i += 1
            if i == len(payload) or payload[i] != "#":
                break
            i += 1
            while i < len(payload) and payload[i].isspace():
                i += 1
        fields[key[1].lower()] = "".join(values)
    return fields


def _bib_text(value):
    return " ".join(re.sub(r"\\(?:textit|emph|textbf|textnormal)\b|[{}]", "", value).split())


def parse_bibtex(text):
    text = re.sub(r"(?m)(?<!\\)%[^\n]*", "", text)
    head = re.compile(r"@([a-z]+)\s*([{(])", re.I)
    entries, macros = [], MONTHS.copy()
    pos = 0
    while match := head.search(text, pos):
        start = match.end() - 1
        end = _close(text, start, match[2], "}" if match[2] == "{" else ")")
        pos = end + 1
        kind = match[1].lower()
        payload = text[start + 1 : end]
        if kind in ("comment", "preamble"):
            continue
        if kind == "string":
            macros.update(_fields(payload, macros))
            continue
        key, comma, fields = payload.partition(",")
        if not comma or not key.strip():
            raise ValueError("BibTeX entry needs a citation key and comma")
        parsed = {k: _bib_text(v) for k, v in _fields(fields, macros).items()}
        parsed.update(id=key.strip(), raw=text[match.start() : pos], entry_type=kind)
        entries.append(reference(parsed))
    return entries


def pdf_text(path):
    program = shutil.which("pdftotext")
    if program is None:
        raise ValueError(
            "PDF input needs pdftotext (Poppler or MiKTeX). Supply extracted text if unavailable."
        )
    args = [program, "-layout", "-enc", "UTF-8", str(path.resolve()), "-"]
    result = subprocess.run(args, capture_output=True, timeout=90, check=False)
    if result.returncode or not result.stdout.strip():
        raise ValueError("The PDF has no usable text layer. Supply OCR or a text export.")
    return result.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def local_text(path):
    if path.suffix.lower() == ".pdf":
        return pdf_text(path)
    return path.read_text(encoding="utf-8-sig")


def _numbers(group):
    for value in re.split(r"[,;]", group):
        bits = re.split(r"[-\u2013\u2014]", value)
        first = int(bits[0])
        last = int(bits[-1])
        if 0 <= last - first <= 50:
            yield from (str(i) for i in range(first, last + 1))


FIRST_PERSON = re.compile(r"\b(?:we|our|us|ours)\b", re.I)
# Marks of the authors' own result: first person or reported statistics.
OWN_RESULT = re.compile(
    r"\b(?:we|our|us)\b|odds ratio|\b95\s*%\s*CI\b|confidence interval|\bp\s*[<=>]\s*0?\.\d|"
    r"\bthis (?:paper|study|work)\b",
    re.I,
)
CLAUSE_BREAK = re.compile(r";\s+|,\s+(?=(?:and|but|while|whereas|yet|so|although)\s)|\s+(?=(?:while|whereas)\s)")


def _cited_clause(statement):
    """When a sentence reports the authors' own result and then cites prior work for a
    final clause ("our rates show no difference, and crawls also find X [5]"), the
    citation covers the final clause only. Return that clause; otherwise the statement."""
    # Split only outside parentheses, so "(19.6%; odds ratio 1.07)" stays one piece.
    parts, depth, last = [], 0, 0
    for match in re.finditer(r"[()]|" + CLAUSE_BREAK.pattern, statement):
        if match[0] in "()":
            depth = max(0, depth + (1 if match[0] == "(" else -1))
        elif depth == 0:
            parts.append(statement[last : match.start()])
            last = match.end()
    parts.append(statement[last:])
    if len(parts) < 2:
        return statement
    tail = []
    for part in reversed(parts):
        if OWN_RESULT.search(part):
            break
        tail.insert(0, part)
    if len(tail) == len(parts) or not tail:
        return statement
    clause = ", ".join(tail)
    clause = re.sub(r"^(?:and|but|while|whereas|yet|so|although)\s+", "", clause, flags=re.I)
    return clause if len(clause.split()) >= 4 else statement


def _mentions(body, latex=False):
    claims = []
    matcher = (
        LATEX_CITE
        if latex
        else re.compile(r"\[(?:\d+(?:\s*[-\u2013\u2014,;]\s*\d+)*|[^\]\n]*@[\w:./-]+[^\]\n]*)\]")
    )
    for piece in re.split(r"(?<=[.!?])\s+(?=(?:[A-Z0-9\\\[]|#{1,6}\s))|\n\s*\n", body):
        markers = list(matcher.finditer(piece))
        if not markers:
            continue
        for i, marker in enumerate(markers):
            previous = markers[i - 1].end() if i else 0
            before = piece[previous : marker.start()].strip(" \n.,;")
            statement = before if len(before.split()) >= 3 else piece[: marker.start()].strip()
            after = piece[marker.end() :].strip(" \n.,;")
            if after and (
                not statement
                or re.search(r"\b(?:in|by|from|according to|see)$", before, re.I)
                # "We use AIDev [1], a dataset of ...": the appositive describes the source.
                or (FIRST_PERSON.search(before) and len(before.split()) <= 6
                    and re.match(r",\s*(?:a|an|the)\b", piece[marker.end() :]))
            ):
                statement = piece.strip()
            scoped = _cited_clause(statement)
            # Keep the whole sentence only when the claim was cut down to one clause.
            sentence = " ".join(piece.split()) if scoped != statement else ""
            statement = scoped
            if latex:
                references = re.split(r"\s*,\s*", marker[1].strip())
            elif numeric := NUMERIC.fullmatch(marker[0]):
                references = list(_numbers(numeric[1]))
            else:
                references = PANDOC.findall(marker[0])
            if statement and references:
                text = " ".join(matcher.sub("", statement).replace(" ,", ",").split())
                claims.append(
                    {
                        "id": f"claim-{len(claims) + 1}",
                        "text": text,
                        "references": list(dict.fromkeys(references)),
                        **({"sentence": sentence} if sentence else {}),
                        **({"first_person": True} if FIRST_PERSON.search(text) else {}),
                    }
                )
    return claims


def _numbered(raw_refs):
    if stop := REF_STOP.search(raw_refs):
        raw_refs = raw_refs[: stop.start()]
    items = list(REF_ITEM.finditer(raw_refs))
    if items:
        bracketed = items[0][1] is not None
        items = [item for item in items if (item[1] is not None) == bracketed]
    refs = []
    for i, item in enumerate(items):
        end = items[i + 1].start() if i + 1 < len(items) else len(raw_refs)
        raw = " ".join(raw_refs[item.end() : end].split())
        refs.append(reference({"id": item[1] or item[2], "raw": raw}))
    return refs


def _latex_refs(body):
    refs = []
    for block in TEX_BIB.finditer(body):
        text = block[1]
        items = list(BIB_ITEM.finditer(text))
        for i, item in enumerate(items):
            end = items[i + 1].start() if i + 1 < len(items) else len(text)
            raw = _bib_text(text[item.end() : end].replace(r"\url", " "))
            refs.append(reference({"id": item[1].strip(), "raw": raw}))
    return refs, TEX_BIB.sub("", body)


def _from_parsed_paper(data):
    sentences = {item["id"]: item["text"] for item in data.get("sentences", [])}
    claims = []
    for item in data.get("citations", data.get("citation_mentions", [])):
        text = sentences.get(item.get("sentence_id"))
        if text:
            ids = list(item.get("bib_ids") or item.get("bibliography_ids") or [])
            if not ids:
                ids = [f"unlinked:{item['id']}"]
            claims.append({"id": item["id"], "text": text, "references": ids})
    return [reference(row) for row in data["bibliography"]], claims


TEX_INPUT = re.compile(r"\\(?:input|include|subfile)\s*\{([^{}]+)\}")
TEX_DEFINE = re.compile(r"\\(?:(?:new|renew|provide)command\*?|def)\s*(?:\{\s*)?\\([A-Za-z@]+)\s*\}?\s*(?:\[(\d)\])?\s*(?:\[[^\]]*\])?\s*(?=\{)")
TEX_LAYOUT = re.compile(r"\\(?:par|vspace|hspace|fbox|fcolorbox|colorbox|parbox|framebox|begin|noindent|centering)(?![A-Za-z])")
TEX_UNWRAP = re.compile(
    r"\\(?:emph|textbf|textit|texttt|textsc|textrm|textsf|mbox|hbox|url|nolinkurl|"
    r"textnormal|underline|path)\s*\{((?:[^{}]|\{[^{}]*\})*)\}"
)
TEX_HEADING = re.compile(
    r"\\(?:part|chapter|section|subsection|subsubsection|paragraph|subparagraph|caption)"
    r"\*?\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^{}]*\})*)\}"
)


def _tex_uncomment(text):
    return re.sub(r"(?m)(?<!\\)%[^\n]*", "", text)


def _tex_inline(body, folder, depth=0):
    """Replace \\input and \\include with the files' text, so claims and macros defined
    in included files are seen. Only files inside the manuscript folder are read."""
    body = _tex_uncomment(body)
    if depth > 4:
        return body

    def include(match):
        name = match[1].strip()
        target = (folder / name).resolve()
        if target.suffix != ".tex":
            target = target.with_name(target.name + ".tex")
        if not target.is_file() or folder.resolve() not in target.parents:
            return match[0]
        return _tex_inline(target.read_text(encoding="utf-8", errors="replace"), folder, depth + 1)

    return TEX_INPUT.sub(include, body)


def _tex_group(text, start):
    """Return (content, end) of the balanced {...} group that opens at text[start]."""
    depth, i = 0, start
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i], i + 1
        i += 1
    return text[start + 1 :], len(text)


def _tex_args(text, start, count):
    args, i = [], start
    for _ in range(count):
        while i < len(text) and text[i] in " \t\n":
            i += 1
        if i >= len(text) or text[i] != "{":
            return None, start
        arg, i = _tex_group(text, i)
        args.append(arg)
    return args, i


def _tex_plain(body):
    """Expand macros defined in the manuscript (often generated numbers) and strip
    markup a reader never sees, so claims read as the PDF does."""
    macros, kept, last = {}, [], 0
    for m in TEX_DEFINE.finditer(body):
        if m.start() < last:
            continue
        value, end = _tex_group(body, m.end())
        macros[m[1]] = (int(m[2] or 0), value)
        kept.append(body[last : m.start()])
        last = end
    body = "".join(kept) + body[last:]
    if macros:
        names = "|".join(map(re.escape, sorted(macros, key=len, reverse=True)))
        pattern = re.compile(r"\\(" + names + r")(?![A-Za-z@])[ \t]?(?:\{\})?")
        for _ in range(4):
            out, i, changed = [], 0, False
            for m in pattern.finditer(body):
                if m.start() < i:
                    continue
                count, value = macros[m[1]]
                args, end = _tex_args(body, m.end(), count) if count else ([], m.end())
                if args is None:
                    continue
                if count and TEX_LAYOUT.search(value):
                    value = " ".join(args)  # a display wrapper: keep only its text
                else:
                    for n, arg in enumerate(args, 1):
                        value = value.replace(f"#{n}", arg)
                out += [body[i : m.start()], value, " "]
                i, changed = end, True
            body = "".join(out) + body[i:]
            if not changed:
                break
    body = TEX_HEADING.sub(lambda m: "\n\n" + m[1] + ".\n\n", body)
    body = re.sub(r"\\label\s*\{[^{}]*\}", "", body)
    for _ in range(3):
        body = TEX_UNWRAP.sub(lambda m: m[1], body)
    for raw, plain in (("~", " "), ("\\%", "%"), ("\\&", "&"), ("\\#", "#"), ("\\xspace", " "),
                       ("\\ ", " ")):
        body = body.replace(raw, plain)
    body = re.sub(r"\\textquotesingle(?![A-Za-z])\s?", "'", body)
    body = re.sub(r"[ \t]+", " ", body)
    return re.sub(r" +([.,;:!?)])", r"\1", body)


def _relative_bibs(body, path):
    paths = []
    for match in re.finditer(r"\\(?:bibliography|addbibresource)(?:\[[^\]]*\])?\{([^{}]+)\}", body):
        for name in match[1].split(","):
            relative = Path(name.strip())
            location = (
                path.parent / (relative if relative.suffix else relative.with_suffix(".bib"))
            ).resolve()
            if location.is_relative_to(path.parent.resolve()) and location.is_file():
                paths.append(location)
    return paths


def read_input(path, bibs, keep=None):
    """`keep`, if given, receives the plain manuscript body as keep["body"]."""
    suffix = path.suffix.lower()
    text = local_text(path)
    notices = []
    claims = []
    refs = []
    if suffix == ".json":
        data = json.loads(text)
        if "bibliography" in data:
            refs, claims = _from_parsed_paper(data)
        else:
            refs = [reference(row) for row in data.get("references", [])]
            claims = data.get("claims", [])
    elif suffix == ".bib":
        refs = parse_bibtex(text)
    elif suffix in (".tex", ".md", ".markdown", ".txt", ".pdf"):
        heading = REF_HEADING.search(text)
        body = text[: heading.start()] if heading else text
        if heading:
            refs = _numbered(text[heading.end() :])
        if suffix == ".tex":
            body = _tex_inline(body, path.parent)
            bibs = list(dict.fromkeys([*bibs, *_relative_bibs(body, path)]))
            inline_refs, body = _latex_refs(body)
            refs.extend(inline_refs)
            body = _tex_plain(body)
        claims = _mentions(body, latex=suffix == ".tex")
        if keep is not None:
            keep["body"] = body
        notices.append(
            "Automatic citation mapping is heuristic. Check coverage against the manuscript."
        )
    else:
        raise ValueError("Expected .bib, .json, .tex, .md, .txt or .pdf input")
    for bib in bibs:
        refs.extend(parse_bibtex(local_text(bib)))
    seen = {}
    for ref in refs:
        if not ref["id"].strip() or (ref["id"] in seen and ref != seen[ref["id"]]):
            raise ValueError(f"Empty or conflicting reference key: {ref['id']!r}")
        seen[ref["id"]] = ref
    claim_ids = set()
    for claim in claims:
        claim_id = claim["id"]
        if not isinstance(claim_id, str) or not claim_id or claim_id in claim_ids:
            raise ValueError(f"Invalid or duplicate claim id: {claim_id!r}")
        claim_ids.add(claim_id)
        if (
            not isinstance(claim["text"], str)
            or not claim["text"].strip()
            or not isinstance(claim["references"], list)
            or not claim["references"]
            or not all(isinstance(key, str) and key.strip() for key in claim["references"])
        ):
            raise ValueError(f"Invalid claim text or reference list: {claim_id}")
        for key in claim["references"]:
            if key not in seen:
                notices.append(f"No bibliography entry for citation {key}.")
                seen[key] = reference({"id": key})
    if not seen:
        notices.append(
            "No reference entries recognized. Supply a bibliography or a claim manifest."
        )
    if not claims:
        notices.append(
            "No claim-to-reference links recognized. This run can check bibliography only."
        )
    return list(seen.values()), claims, notices
