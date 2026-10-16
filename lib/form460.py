"""Form 460 reading helpers for the clerk review (stdlib only).

Reads the boxes a clerk checks on a paper Form 460:
  * Schedule A / C summary box: line 1 itemized, line 2 unitemized, line 3 total (1 + 2 = 3).
  * Schedule E summary box: line 1 itemized payments, 2 unitemized, 3 interest, 4 total (1 + 2 + 3 = 4).
  * Summary Page column A lines 1 and 6 (cross-check only).
All amounts are integer cents. Every function is pure (text in, numbers out) so it can be unit tested.
"""
import re

MONEY = re.compile(r"(?<![\d/])\(?-?\$?\s?(\d{1,3}(?:[,.]\d{3})+|\d+)[.,](\d{2})(?![\d])\)?")
MONEY_INT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?![\d.,/])")
ZERO = re.compile(r"-\s*[0OQ]\s*-")


def cents_tokens(s):
    """Every money amount in s as integer cents, in order. '-0-' counts as 0."""
    s = ZERO.sub(" 0.00 ", s or "")
    out = []
    for m in MONEY.finditer(s):
        whole = m.group(1).replace(",", "").replace(".", "")
        out.append((m.start(), int(whole) * 100 + int(m.group(2))))
    if not out:
        for m in MONEY_INT.finditer(s):
            out.append((m.start(), int(m.group(1).replace(",", "")) * 100))
    return [v for _, v in sorted(out)]


def fmt(c):
    """12345 -> '123.45' (no thousands separator)."""
    return f"{c // 100}.{c % 100:02d}"


def fmt_variants(c):
    d, r = c // 100, c % 100
    return {f"{d}.{r:02d}", f"{d:,}.{r:02d}", f"{d:,}.{r:02d}".replace(",", ".")}


def text_has_amount(text, c):
    """True when the page text holds this exact amount as a whole number (any common print form).
    '6,930.00' inside '16,930.00' does not count."""
    if c in cents_tokens(text):
        return True
    return any(re.search(r"(?<![\d,.])" + re.escape(v) + r"(?![\d])", text or "") for v in fmt_variants(c))


SUMMARY_ANCHOR = {
    "A": re.compile(r"sch\w{0,6}\s*[-_.]?\s*A\s*[-_.]?\s*summ?ary", re.I),
    "C": re.compile(r"sch\w{0,6}\s*[-_.]?\s*C\s*[-_.]?\s*summ?ary", re.I),
    "E": re.compile(r"sch\w{0,6}\s*[-_.]?\s*E\s*[-_.]?\s*summ?ary", re.I),
}
_END = re.compile(r"FPPC\s*Form\s*4\s*6\s*0|FPPC\s*Advice|www\.\s*fppc", re.I)

# label line patterns per schedule, in form order (index = line number - 1)
LABELS = {
    "A": [re.compile(r"(?<!un)(?<!un )itemi[sz]ed\s*mon|monetary\s*contrib\w*\s*of\s*\$?\s*100\s*or\s*more|(^|\s)1\s*[.,]\s*amount", re.I),
          re.compile(r"un\s*-?\s*itemi[sz]ed|less\s*than\s*\$?\s*100|(^|\s)2\s*[.,]\s*amount", re.I),
          re.compile(r"total\s*mon\w*\s*contrib|(^|\s)3\s*[.,]\s*total", re.I)],
    "C": [re.compile(r"(?<!un)(?<!un )itemi[sz]ed\s*non|non\s*-?\s*mon\w*\s*contrib\w*\s*of\s*\$?\s*100\s*or\s*more|(^|\s)1\s*[.,]\s*amount", re.I),
          re.compile(r"un\s*-?\s*itemi[sz]ed|less\s*than\s*\$?\s*100|(^|\s)2\s*[.,]\s*amount", re.I),
          re.compile(r"total\s*non\w*\s*contrib|(^|\s)3\s*[.,]\s*total", re.I)],
    "E": [re.compile(r"(?<!un)(?<!un )(it|h|l)?emi[sz]ed\s*pay|payments\s*(made\s*)?(this\s*period\s*)?of\s*\$?\s*100\s*or\s*more|(^|\s)1\s*[.,]+\s*\S*\s*pay", re.I),
          re.compile(r"un\s*-?\s*itemi[sz]ed|under\s*\$?\s*100|(^|\s)2\s*[.,]+\s*un", re.I),
          re.compile(r"interest\s*paid|(^|\s)3\s*[.,]\s*total\s*interest", re.I),
          re.compile(r"total\s*payments\s*made|(^|\s)4\s*[.,]\s*total", re.I)],
}


def find_box(text, sched):
    """Text of the schedule's summary box, or None."""
    m = SUMMARY_ANCHOR[sched].search(text or "")
    if not m:
        return None
    rest = text[m.end():]
    e = _END.search(rest)
    return rest[: min(e.start(), 3000)] if e else rest[:3000]


def read_box(text, sched):
    """Read a schedule summary box. Returns dict(lines=[c1, c2, ...], arith=bool, found=True) or None when no box.

    Each value is taken from the text between its label line and the next label line (blank or '-0-' = 0).
    arith is True when the box adds up (A/C: 1 + 2 = 3; E: 1 + 2 + 3 = 4).
    """
    box = find_box(text, sched)
    if box is None:
        return None
    lines = box.split("\n")
    labels = LABELS[sched]
    at, pos = [], 0
    for pat in labels:
        hit = None
        for i in range(pos, len(lines)):
            if pat.search(lines[i]):
                hit = i
                break
        at.append(hit)
        if hit is not None:
            pos = hit + 1
    if at[0] is None:
        return {"found": True, "lines": None, "arith": False, "why": "line 1 label not read"}
    vals = []
    for k, start in enumerate(at):
        if start is None:
            vals.append(None)
            continue
        nxt = next((a for a in at[k + 1:] if a is not None), len(lines))
        seg = "\n".join(lines[start:nxt])
        # drop the '$100' in the wording and 'Lines 1 and 2' style references
        seg = re.sub(r"\$?\s?100\s*or\s*more|less\s*than\s*\$?\s?100|under\s*\$?\s?100|of\s*\$\s?100", " ", seg, flags=re.I)
        seg = re.sub(r"lines?\s*\d+(\s*(,|and|&|\+)\s*\d+)*", " ", seg, flags=re.I)
        toks = cents_tokens(seg)
        vals.append(toks[-1] if (k == len(at) - 1 and toks) else (toks[0] if toks else 0))
    total, parts = vals[-1], vals[:-1]
    arith = total is not None and all(p is not None for p in parts) and sum(parts) == total
    if not arith and total is not None and parts[0] is not None:
        # a value printed a line low can slide into the next segment: fit the box's amounts in order
        toks = cents_tokens(box)[:12]
        n = len(labels)
        for i in range(len(toks)):
            for j in range(i + 1, len(toks)):
                if n == 3:
                    for mid in [0] + toks[i + 1:j]:
                        if toks[i] + mid == toks[j]:
                            return {"found": True, "lines": [toks[i], mid, toks[j]], "arith": True, "why": "order-fit"}
                else:
                    if toks[i] == toks[j]:
                        return {"found": True, "lines": [toks[i], 0, 0, toks[j]], "arith": True, "why": "order-fit"}
                    for a in toks[i + 1:j]:
                        if toks[i] + a == toks[j]:
                            return {"found": True, "lines": [toks[i], a, 0, toks[j]], "arith": True, "why": "order-fit"}
    return {"found": True, "lines": vals, "arith": arith, "why": None}


SUMLINES = {1: re.compile(r"(?<!non)(?<!non )(?<!non-)monetary\s*contrib", re.I),
            6: re.compile(r"(?<!cash )payments?\s*made", re.I)}


def is_summary_page(text):
    head = "\n".join((text or "").split("\n")[:12])
    return bool(re.search(r"SUMMARY PAGE", head, re.I)) or ("ENDING CASH" in (text or "").upper() and "Monetary Contributions" in (text or ""))


def read_summary_page(text):
    """{1: cents, 6: cents} column A from the Summary Page (cross-check only)."""
    out = {}
    for n, pat in SUMLINES.items():
        for ln in (text or "").split("\n"):
            l2 = re.sub(r"\s+", " ", ln)
            if not pat.search(l2) or not re.match(r"^\W{0,4}[\dJBIlO]{1,2}\s*[.,:]?\s*[A-Za-z]", l2.strip()):
                continue
            body = re.sub(r"^\W*[\dJBIlO]{1,2}\s*[.,:]", " ", l2)
            toks = cents_tokens(body)
            if toks:
                out[n] = toks[0]
            break
    return out


# --- OCR misread repair candidates -------------------------------------------------
_PAIRS = ["08", "06", "17", "14", "27", "38", "35", "56", "58", "68", "49", "36"]
CONFUSE = {d: "".join(sorted({p[1 - p.index(d)] for p in _PAIRS if d in p})) for d in "0123456789"}


def misread_variants(c):
    """Amounts an OCR engine could plausibly have produced instead of c (or c instead of them):
    decimal shift, one confusable digit, adjacent transposition, one digit dropped or doubled."""
    s = str(c)
    out = set()
    for f in (10, 100):
        out.add(c * f)
        if c % f == 0:
            out.add(c // f)
    for i, ch in enumerate(s):
        for alt in CONFUSE.get(ch, ""):
            out.add(int(s[:i] + alt + s[i + 1:]))
        if i + 1 < len(s) and s[i] != s[i + 1]:
            out.add(int(s[:i] + s[i + 1] + s[i] + s[i + 2:]))
        if len(s) > 1:
            out.add(int(s[:i] + s[i + 1:] or "0"))
        out.add(int(s[:i] + ch + s[i:]))
    out.discard(c)
    return {v for v in out if v > 0}
