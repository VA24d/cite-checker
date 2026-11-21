"""Deterministic number facts for claim-versus-source checks.

LLM judges are poor at arithmetic, so this module extracts numbers from a claim and its
source, compares them (exact, rounded, derived, mismatch candidates, absent) and flags
number problems inside a manuscript (abstract numbers missing from the body, one quantity
stated with two values). Standard library only. Years used as dates, section and table
numbers, and citation numbers like [12] are not extracted.
"""

import re
from decimal import ROUND_HALF_UP, Decimal

MAX_SOURCE_NUMBERS = 400
MAX_DERIVED_POOL = 300

STOP = frozenset(
    "the and for with from that this were was are has had have been not but its their than then "
    "also into over under between among such these those which while when where who whom per via "
    "our can may will would could should about each both more most less very only any all one two".split()
)
MONTHS = frozenset(
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec".split()
)
DATE_WORDS = frozenset("in since by before after during until till from of circa year years early late mid".split())
COUNT_NOUNS = frozenset(
    "patients participants samples images examples users subjects people adults children men women "
    "papers articles studies trials cases records items instances respondents students".split()
)

_MULT = {"k": 1000, "thousand": 1000, "million": 1000000, "billion": 1000000000}
_REF_BEFORE = re.compile(
    r"(?:section|sec\.?|chapter|appendix|table|fig(?:ure)?\.?|eqs?\.?|equations?|algorithm|theorem|"
    r"lemma|step|page|pp?\.|§|line|proposition|corollary|definition|exhibit|panel)\s*\(?$",
    re.I,
)
_CI_BEFORE = re.compile(r"(?:\bCI|confidence interval|credible interval|\bCrI|\bIQR|interval)\W{0,6}[\w\s%:=]{0,12}$", re.I)
_CI_AFTER = re.compile(r"^\s?(?:%\s?)?(?:CI\b|confidence|credible)", re.I)
_CITATION = re.compile(r"\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\]")
_SECTION_LINE = re.compile(r"^[ \t]*(?:#+[ \t]*)?(\d+(?:\.\d+)*)\.?[ \t]+(?=[A-Z])", re.M)
_NUM = re.compile(
    r"(?P<pre>×\s?|(?<![\w.])x(?=\d))?"
    r"(?<![\w.])(?P<sign>[-−](?=\d))?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<frac>\d+))?"
    r"(?P<suf>\s?%|\s?percent\b|[kK]\b|\s?(?:thousand|million|billion)\b|[×x](?![\w]))?"
)
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_ALPHA = re.compile(r"[A-Za-z]{3,}")
_SENT = re.compile(r"[.!?;]\s+(?=[A-Z])|\n\s*\n")


def _clean(text):
    t = text or ""
    t = re.sub(r"\\cite\w*\*?(?:\[[^\]]*\])*\{[^}]*\}", " ", t)
    t = re.sub(r"\\(?:ref|eqref|label|autoref|cref|Cref|pageref)\{[^}]*\}", " ", t)
    t = t.replace("\\%", "%").replace("\\times", "×").replace("\\pm", "±").replace("\\,", " ")
    t = t.replace("\\;", " ").replace("\\ ", " ").replace("~", " ").replace("$", "")
    t = t.replace("\u00a0", " ").replace("\u2009", " ").replace("\u202f", " ").replace("\u2212", "-")
    t = _CITATION.sub(" ", t)
    t = re.sub(r"(?<=\d)\s*(?:--|—|---)\s*(?=-?\d)", " – ", t)
    return t


def _dec(x):
    return Decimal(str(x))


def _ctx_words(chunk):
    return {w[:5] for w in (m.lower() for m in _ALPHA.findall(chunk)) if w not in STOP}


def _words(chunk):
    return [m for m in _WORD.findall(chunk)]


def _before_chunk(text, start):
    chunk = text[max(0, start - 90) : start]
    parts = _SENT.split(chunk)
    return parts[-1] if parts else chunk


def _after_chunk(text, end):
    chunk = text[end : end + 90]
    m = _SENT.search(chunk)
    return chunk[: m.start() + 1] if m else chunk


def group(kind):
    if kind == "percent":
        return "percent"
    if kind == "multiplier":
        return "mult"
    if kind.startswith("ci_"):
        return "ci"
    return "num"


def extract(text):
    """Return numbers found in text as dicts (value, kind, text, decimals, step, context)."""
    t = _clean(text)
    skip_starts = {m.start(1) for m in _SECTION_LINE.finditer(t)}
    out = []
    for m in _NUM.finditer(t):
        start, end = m.start(), m.end()
        intpart = m.group("int")
        s_start = m.start("int")
        if s_start in skip_starts:
            continue
        before_raw = t[max(0, start - 18) : start]
        if _REF_BEFORE.search(before_raw):
            continue
        prev = t[start - 1] if start else ""
        if not m.group("sign") and prev == "-" and start >= 2 and t[start - 2].isalpha():
            continue  # GPT-4, COVID-19
        suf = (m.group("suf") or "").strip()
        pre = m.group("pre")
        nxt_text = t[end:]
        if not suf and re.match(r"(?:st|nd|rd|th|D)\b", nxt_text):
            continue
        if _CI_AFTER.match(nxt_text) and suf in ("", "%", "percent"):
            continue  # the "95" of "95% CI"
        frac = m.group("frac")
        mant = Decimal(intpart.replace(",", "") + ("." + frac if frac else ""))
        if m.group("sign"):
            mant = -mant
        decimals = len(frac) if frac else 0
        mult = 1
        kind = None
        if suf in ("%", "percent"):
            kind = "percent"
        elif suf.lower() in _MULT:
            mult = _MULT[suf.lower()]
        elif suf in ("×", "x") or pre:
            kind = "multiplier"
        after_words = _words(_after_chunk(t, end))
        first_after = after_words[0].lower() if after_words else ""
        lead = before_raw.strip().split()
        prev_word = lead[-1].lower().strip("(,") if lead else ""
        if (
            kind is None
            and not frac
            and "," not in intpart
            and mult == 1
            and len(intpart) == 4
            and 1900 <= int(intpart) <= 2099
            and first_after not in COUNT_NOUNS
        ):
            continue  # year used as a date
        if (first_after in MONTHS and not frac and kind is None) or (
            prev_word in MONTHS and not frac and kind is None
        ):
            continue
        value = float(mant * mult)
        step = float(Decimal(1).scaleb(-decimals) * mult)
        if kind is None:
            if frac:
                kind = "ratio"
            else:
                kind = "count" if first_after and first_after not in STOP else "plain"
        before = _before_chunk(t, start)
        after = _after_chunk(t, end)
        bw, aw = _words(before), _words(after)
        out.append(
            {
                "value": value,
                "kind": kind,
                "text": t[start:end].strip(),
                "decimals": decimals,
                "step": step,
                "unit": " ".join(aw[:2]),
                "before": " ".join(bw[-3:]),
                "after": " ".join(aw[:3]),
                "context": sorted(_ctx_words(" ".join(bw[-3:]) + " " + " ".join(aw[:3]))),
                "pos": start,
                "_end": end,
            }
        )
    _mark_ci(t, out)
    for n in out:
        n.pop("_end", None)
    return out


def _mark_ci(t, nums):
    i = 0
    while i < len(nums) - 1:
        a, b = nums[i], nums[i + 1]
        gap = t[a["_end"] : b["pos"]]
        if re.fullmatch(r"\s*(?:–|-|to|,|;|—)\s*", gap) and _CI_BEFORE.search(t[max(0, a["pos"] - 40) : a["pos"]]):
            a["kind"], b["kind"] = "ci_low", "ci_high"
            i += 2
        else:
            i += 1


def _round_to(value, step):
    q = (_dec(value) / _dec(step)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return float(q * _dec(step))


def _same(a, b):
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def _is_exact(c, s):
    return _same(c["value"], s["value"])


def _is_rounded(c, s):
    if s["step"] >= c["step"] or _same(c["value"], s["value"]):
        return False
    return _same(_round_to(s["value"], c["step"]), c["value"])


def _compatible(c, s):
    gc, gs = group(c["kind"]), group(s["kind"])
    if gc == gs:
        return True
    # tables often drop the % sign
    return gc == "percent" and gs == "num"


def _brief(n):
    return {k: n[k] for k in ("text", "value", "kind", "before", "after")}


def _claim_words(text):
    return _ctx_words(_clean(text))


def _relevance(n, words):
    return len(set(n["context"]) & words)


def _derive(c, pool, words):
    v, d = c["value"], c["decimals"]
    tol = c["step"] / 2 + 1e-9
    g = group(c["kind"])
    loose = c["step"] >= 1 and g != "mult"
    if g == "ci":
        return []
    if g == "num" and d == 0 and abs(v) < 10:
        return []
    nums = [n for n in pool if group(n["kind"]) == "num" and n["value"] != 0]
    pcts = [n for n in pool if n["kind"] == "percent"]
    hits = []

    def fmt(x):
        return f"{x:.{d}f}"

    def add(formula, val, a, b):
        err = abs(val - v)
        if err <= tol:
            rel = _relevance(a, words) + _relevance(b, words)
            if loose and rel == 0:
                return
            hits.append((err, -rel, formula))

    def g_(x):
        return f"{x:g}"

    if g == "percent":
        for a in nums:
            for b in nums:
                if a is b or a["value"] < 0 or b["value"] <= 0 or a["value"] > b["value"] * 100:
                    continue
                x, y = a["value"], b["value"]
                add(f"{g_(x)}/{g_(y)} = {fmt(x / y * 100)}%", x / y * 100, a, b)
                if x < y:
                    add(f"1 - {g_(x)}/{g_(y)} = {fmt((1 - x / y) * 100)}%", (1 - x / y) * 100, a, b)
                if x != 0:
                    ch = (y - x) / x * 100
                    if abs(ch) <= 1e4:
                        add(f"({g_(y)}-{g_(x)})/{g_(x)} = {fmt(ch)}%", abs(ch), a, b)
        for a in pcts:
            for b in pcts:
                if a is b:
                    continue
                x, y = a["value"], b["value"]
                if x > y:
                    add(f"{g_(x)}-{g_(y)} = {fmt(x - y)} percentage points", x - y, a, b)
                if x != 0 and x < y:
                    ch = (y - x) / x * 100
                    add(f"({g_(y)}-{g_(x)})/{g_(x)} = {fmt(ch)}%", ch, a, b)
                if x != 0 and x > y:
                    ch = (x - y) / x * 100
                    add(f"({g_(x)}-{g_(y)})/{g_(x)} = {fmt(ch)}%", ch, a, b)
    else:
        for a in nums:
            for b in nums:
                if a is b:
                    continue
                x, y = a["value"], b["value"]
                if y != 0 and x > 0 and y > 0 and x >= y:
                    add(f"{g_(x)}/{g_(y)} = {fmt(x / y)}", x / y, a, b)
                if g == "num" and x > y:
                    add(f"{g_(x)}-{g_(y)} = {fmt(x - y)}", x - y, a, b)
        if g == "num":
            for a in pcts:
                for b in pcts:
                    if a is not b and a["value"] > b["value"]:
                        x, y = a["value"], b["value"]
                        add(f"{g_(x)}-{g_(y)} = {fmt(x - y)} percentage points", x - y, a, b)
    hits.sort()
    seen, out = set(), []
    for _, _, f in hits:
        if f not in seen:
            seen.add(f)
            out.append(f)
        if len(out) == 3:
            break
    return out


def _mismatches(c, srcs):
    out = []
    cg = group(c["kind"])
    cctx = set(c["context"])
    need = 1 if cg in ("percent", "mult") else 2
    for s in srcs:
        if group(s["kind"]) != cg or _is_exact(c, s) or _is_rounded(c, s):
            continue
        shared = cctx & set(s["context"])
        if len(shared) >= need or (shared and min(len(cctx), len(s["context"])) <= 1):
            out.append((-len(shared), s))
    out.sort(key=lambda p: p[0])
    return [_brief(s) | {"shared_words": sorted(set(c["context"]) & set(s["context"]))} for _, s in out[:5]]


def compare(claim_text, source_text):
    claim_nums = [n for n in extract(claim_text)]
    words = _claim_words(claim_text)
    src = extract(source_text)
    if len(src) > MAX_SOURCE_NUMBERS:
        ranked = sorted(range(len(src)), key=lambda i: (-_relevance(src[i], words), i))[:MAX_SOURCE_NUMBERS]
        src = [src[i] for i in sorted(ranked)]
    pool = src
    if len(pool) > MAX_DERIVED_POOL:
        pool = sorted(src, key=lambda n: -_relevance(n, words))[:MAX_DERIVED_POOL]
    results = []
    summary = {"absent": [], "derived": [], "mismatch": []}
    for c in claim_nums:
        entry = {"claim": _brief(c) | {"decimals": c["decimals"], "unit": c["unit"]}}
        exact = [s for s in src if _compatible(c, s) and _is_exact(c, s)]
        rounded = [s for s in src if _compatible(c, s) and _is_rounded(c, s)]
        entry["exact"] = [_brief(s) for s in exact[:3]]
        entry["rounded"] = [_brief(s) for s in rounded[:3]]
        entry["derived"] = []
        entry["mismatch_candidates"] = []
        if exact:
            entry["status"] = "exact"
        elif rounded:
            entry["status"] = "rounded"
        else:
            entry["derived"] = _derive(c, pool, words)
            if entry["derived"]:
                entry["status"] = "derived"
                summary["derived"].append({"value": c["text"], "formula": entry["derived"][0]})
            else:
                entry["mismatch_candidates"] = _mismatches(c, src)
                if entry["mismatch_candidates"]:
                    entry["status"] = "mismatch"
                    summary["mismatch"].append(
                        {"value": c["text"], "source_values": [m["text"] for m in entry["mismatch_candidates"]]}
                    )
                else:
                    entry["status"] = "absent"
                    summary["absent"].append(c["text"])
        results.append(entry)
    return {"numbers": results, "summary": summary}


# ---------------------------------------------------------------- consistency

def _worth_checking(n):
    if n["kind"].startswith("ci_"):
        return n["decimals"] > 0
    if n["kind"] in ("percent", "multiplier"):
        return True
    if n["decimals"] > 0:
        return True
    return abs(n["value"]) > 10


def _in_body(n, body):
    for b in body:
        if _same(n["value"], b["value"]) or _is_rounded(n, b):
            return True
        if n["kind"] == "percent" and (_same(n["value"] / 100, b["value"]) or _is_rounded(
            {**n, "value": n["value"] / 100, "step": n["step"] / 100}, b)):
            return True
    return False


def _snippet(n):
    return " ".join(x for x in (n["before"], n["text"], n["after"]) if x)


def consistency(sections):
    """Flag abstract/conclusion numbers missing from the body and restated quantities that differ."""
    body = extract(sections.get("body") or "")
    findings = []
    if not body:
        return findings
    for where in ("abstract", "conclusion"):
        nums = [n for n in extract(sections.get(where) or "") if _worth_checking(n)]
        for n in nums:
            if _in_body(n, body):
                continue
            other = None
            ctx = set(n["context"])
            best = 0
            if len(ctx) >= 3 and n["kind"] != "plain":
                for b in body:
                    if group(b["kind"]) != group(n["kind"]):
                        continue
                    shared = ctx & set(b["context"])
                    if len(shared) >= 3 and len(shared) >= 0.6 * min(len(ctx), len(b["context"])) and len(shared) > best:
                        best, other = len(shared), b
            if other is not None:
                findings.append(
                    {
                        "kind": "inconsistent_number",
                        "value": n["text"],
                        "where": where,
                        "context": _snippet(n),
                        "other": {"where": "body", "value": other["text"], "context": _snippet(other)},
                    }
                )
            else:
                findings.append(
                    {
                        "kind": "abstract_number_missing",
                        "value": n["text"],
                        "where": where,
                        "context": _snippet(n),
                        "other": None,
                    }
                )
    return findings


# ------------------------------------------------------------ section split

_NAMED = r"(?:introduction|background|related work|methods?|methodology|materials and methods|results|discussion|experiments?|approach|preliminaries)"
_END_HEAD = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+.*|\*\*.*\*\*|\d+(?:\.\d+)*\.?[ \t]+[A-Z][^\n]{0,70}|(?:" + _NAMED + r")[ \t]*:?)[ \t]*$",
    re.I | re.M,
)
_REFS = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*|\*\*|\d+\.?[ \t]+)?(?:references|bibliography|acknowledge?ments?|appendix|appendices)\b.*$",
    re.I | re.M,
)
_ABS_HEAD = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*|\*\*)?(?:\d+\.?[ \t]+)?abstract\**[ \t]*:?[ \t]*$", re.I | re.M)
_CONC_HEAD = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*|\*\*)?(?:\d+\.?[ \t]+)?(?:(?:discussion|summary)\s+and\s+)?conclusions?"
    r"(?:\s+and\s+[\w ]{1,30})?\**[ \t]*:?[ \t]*$",
    re.I | re.M,
)


def _latex_split(text):
    doc = text
    m = re.search(r"\\begin\{document\}", doc)
    if m:
        doc = doc[m.end() :]
    tail = re.search(r"\\bibliography\{|\\begin\{thebibliography\}|\\appendix\b|\\end\{document\}|\\printbibliography", doc)
    if tail:
        doc = doc[: tail.start()]
    abstract = ""
    a = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", doc, re.S)
    if a:
        abstract = a.group(1).strip()
        doc = doc[: a.start()] + doc[a.end() :]
    conclusion = ""
    c = re.search(r"\\(?:section|chapter)\*?\{\s*(?:[^}]*\band\s+)?conclusions?\b[^}]*\}", doc, re.I)
    if c:
        nxt = re.search(r"\\(?:section|chapter)\*?\{", doc[c.end() :])
        end = c.end() + nxt.start() if nxt else len(doc)
        conclusion = doc[c.end() : end].strip()
        doc = doc[: c.start()] + doc[end:]
    return abstract, doc.strip(), conclusion


def _plain_split(text):
    abstract = conclusion = ""
    body = text
    a = _ABS_HEAD.search(body)
    if a:
        rest = body[a.end() :]
        e = _END_HEAD.search(rest)
        if e:
            end = e.start()
        else:
            blank = re.search(r"\S\s*\n\s*\n", rest)
            end = blank.end() if blank else len(rest)
        abstract = rest[:end].strip()
        body = body[: a.start()] + rest[end:]
    c = _CONC_HEAD.search(body)
    if c:
        rest = body[c.end() :]
        ends = [x.start() for x in (_END_HEAD.search(rest), _REFS.search(rest)) if x]
        end = min(ends) if ends else len(rest)
        conclusion = rest[:end].strip()
        body = body[: c.start()] + rest[end:]
    r = _REFS.search(body)
    if r:
        body = body[: r.start()]
    return abstract, body.strip(), conclusion


def abstract_and_body(manuscript_text, latex=False):
    """Split a manuscript into abstract, body and conclusion; abstract is empty when not found."""
    text = manuscript_text or ""
    a, b, c = _latex_split(text) if latex else _plain_split(text)
    return {"abstract": a, "body": b, "conclusion": c}
