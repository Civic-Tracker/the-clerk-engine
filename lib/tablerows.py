"""Table-aware row rebuild for a scanned Form 460 schedule page (stdlib only, pure functions).

Input is a page's word boxes (left, top, width, height, conf, text) at 400 dpi, from the PDF text layer or
tesseract TSV. A clerk reads a schedule page as a table:
  * the column headings say where AMOUNT (received this period / paid / fair market value) sits, and where the
    CUMULATIVE column starts (so a cumulative-to-date figure is never taken for this period's amount);
  * every money figure in the AMOUNT column starts one row; the name and date sit on that row's line;
  * the page's SUBTOTAL line, when printed, must equal the page's rows.
A rebuilt schedule only counts when its rows add up to the schedule summary's itemized line (the caller checks).
"""
import re

AMT = re.compile(r"^\(?\$?(\d{1,3}(?:,\d{3})+|\d{1,7})(?:[.,](\d{2}))?\)?[.,;:]?$")
DATE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})")
HEADING = re.compile(r"sch\w{0,6}\s*[-_.]?\s*([A-I])\b", re.I)


def money(tok):
    """'$1,480.05' -> 148005 cents; '500' -> 50000; None for anything that is not a plain amount."""
    t = (tok or "").strip().replace("$", "").replace(" ", "")
    if not t or "/" in t:
        return None
    m = AMT.match(t)
    if not m:
        return None
    whole = int(m.group(1).replace(",", ""))
    if m.group(2) is None and "," not in m.group(1) and len(m.group(1)) > 6:
        return None
    return whole * 100 + int(m.group(2) or 0)


def lines_of(words):
    """Group word boxes into text lines by vertical centre. Returns [{'y': top, 'words': [...] (left to right)}]."""
    ws = sorted(words, key=lambda w: w[1] + w[3] / 2)
    if not ws:
        return []
    hs = sorted(w[3] for w in ws if w[3] > 0)
    tol = max(10, (hs[len(hs) // 2] if hs else 30) * 0.6)
    out, cur, cy = [], [], None
    for w in ws:
        y = w[1] + w[3] / 2
        if cur and abs(y - cy) > tol:
            out.append(cur)
            cur = []
        cur.append(w)
        cy = y if len(cur) == 1 else (cy * (len(cur) - 1) + y) / len(cur)
    if cur:
        out.append(cur)
    return [{"y": min(w[1] for w in l), "yc": sum(w[1] + w[3] / 2 for w in l) / len(l), "words": sorted(l)} for l in out]


def line_text(l):
    return " ".join(w[5] for w in l["words"])


def page_schedule(words, height=None):
    """The schedule letter printed in the page heading (top 18% of the page), or None."""
    if not words:
        return None
    h = height or max(w[1] + w[3] for w in words)
    top = [l for l in lines_of(words) if l["y"] < 0.18 * h]
    for l in top:
        m = HEADING.search(line_text(l))
        if m:
            return m.group(1).upper()
    return None


def find_columns(words, sched):
    """Column x-ranges from the table heading: {'amt': (lo, hi), 'name': (lo, hi), 'date_hi': x, 'head_y': y} or None."""
    if not words:
        return None
    width = max(w[0] + w[2] for w in words)
    height = max(w[1] + w[3] for w in words)
    ls = [l for l in lines_of(words) if l["y"] < 0.55 * height]
    amt = cum = name = code = None
    for l in ls:
        for w in l["words"]:
            t = re.sub(r"[^A-Za-z]", "", w[5]).upper()
            if t == "AMOUNT" and w[0] > 0.45 * width and amt is None:
                amt = w
            elif t in ("FAIR",) and w[0] > 0.45 * width and amt is None and sched == "C":
                amt = w
    if amt is None:
        return None
    ay = amt[1]
    near = [w for l in ls for w in l["words"] if abs(w[1] - ay) < 160]
    for w in near:
        t = re.sub(r"[^A-Za-z]", "", w[5]).upper()
        if t.startswith("CUMULAT") and w[0] > amt[0] and (cum is None or w[0] < cum[0]):
            cum = w
        if t in ("NAME", "FULL") and w[0] < 0.4 * width and (name is None or w[0] < name[0]):
            name = w
        if t in ("CODE", "CODE*", "CONTRIBUTOR", "COOE") and 0.25 * width < w[0] < amt[0] and (code is None or w[0] < code[0]):
            if name is None or w[0] > name[0] + 0.12 * width:
                code = w
    head_y = max(w[1] + w[3] for w in near)
    if sched == "E":
        lo, hi = amt[0] - 0.06 * width, width + 1
    else:
        hi = (cum[0] - 30) if cum else amt[0] + 0.09 * width
        lo = amt[0] - 0.06 * width
    nlo = (name[0] - 0.09 * width) if name else 0.0
    nhi = code[0] - 20 if code else 0.42 * width
    return {"amt": (lo, hi), "name": (max(0.0, nlo), nhi), "date_hi": max(0.0, nlo), "head_y": head_y, "width": width, "height": height}


END = re.compile(r"sub\s*-?\s*totals?|summ?ary|contributor\s*codes|fppc\s*form|fppc\s*advice", re.I)
SUB = re.compile(r"sub\s*-?\s*totals?", re.I)


def parse_date(s):
    m = DATE.search(s or "")
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    if not (1 <= mo <= 12 and 1 <= d <= 31 and 1990 <= y <= 2035):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def clean_name(ws):
    s = " ".join(w[5] for w in ws).strip()
    s = re.split(r"\s*,\s*(?=\S*\d)|\s+\d{2,}", s)[0]          # stop at the street address
    s = re.sub(r"[\s\-_|.,;:]+$", "", s)
    s = re.sub(r"^[\s|_\-.,;:]+", "", s)
    return s


def _box(ws, lo, hi):
    x0 = min(w[0] for w in ws) - 30
    x1 = max(w[0] + w[2] for w in ws) + 30
    y0 = min(w[1] for w in ws) - 15
    y1 = max(w[1] + w[3] for w in ws) + 15
    return (max(lo - 10, x0), y0, min(hi, x1) if hi < 10 ** 6 else x1, y1)


def rebuild_page(words, sched, cols=None, cell_reader=None):
    """Rows of one schedule page. Returns {'rows': [{'amount', 'name', 'date', 'y'}], 'subtotal', 'cols', 'ok'}.
    cell_reader(boxes) -> [text]: re-reads an AMOUNT cell whose first read is not a clean amount (or lost its
    decimal point) as one digits-only line. A row whose amount still cannot be read gets amount None."""
    own = find_columns(words, sched)
    c = own or cols
    if not c:
        return {"rows": [], "subtotal": None, "cols": None, "ok": False, "why": "no_columns"}
    lo, hi = c["amt"]
    nlo, nhi = c["name"]
    head_y = c["head_y"] if own else 0
    body, sub_line = [], None
    for l in lines_of(words):
        if l["y"] <= head_y:
            continue
        txt = line_text(l)
        if SUB.search(txt):
            sub_line = l
            break
        if END.search(txt):
            break
        body.append(l)
    cand = []
    for l in body:
        aw = [w for w in l["words"] if lo <= w[0] < hi]
        if not aw:
            continue
        vals = [(w, money(w[5])) for w in aw]
        good = [(w, v) for w, v in vals if v is not None]
        digits = sum(ch.isdigit() for w in aw for ch in w[5])
        if not good and digits < 2:
            continue
        cand.append((l, aw, good))
    cents_style = sum(1 for _, _, g in cand for w, v in g[:1] if re.search(r"[.,]\d{2}\)?$", w[5])) > len(cand) / 2
    reread, rows = [], []
    for l, aw, good in cand:
        w0, v0 = good[0] if good else (None, None)
        suspect = v0 is None or (cents_style and not re.search(r"[.,]\d{2}\)?\W?$", w0[5]))
        rows.append({"amount": v0, "name": clean_name([w for w in l["words"] if nlo <= w[0] < nhi]),
                     "date": parse_date(" ".join(w[5] for w in l["words"] if w[0] < c["date_hi"] + 40)), "y": l["y"],
                     "_box": _box(aw, lo, hi), "_suspect": suspect})
    if cell_reader:
        sus = [r for r in rows if r["_suspect"]]
        if sus:
            for r, t in zip(sus, cell_reader([r["_box"] for r in sus])):
                v = money((t or "").replace(" ", ""))
                if v is not None and (not cents_style or re.search(r"[.,]\d{2}$", t or "")):
                    r["amount"] = v
                elif r["amount"] is not None and cents_style:
                    r["amount"] = None        # a cents-style column with no decimal point read twice: unread
    else:
        for r in rows:
            if r["_suspect"] and cents_style:
                r["amount"] = None
    subtotal = None
    if sub_line is not None:
        vals = [money(w[5]) for w in sub_line["words"] if lo <= w[0] < hi]
        vals = [v for v in vals if v is not None] or [v for v in (money(w[5]) for w in sub_line["words"]) if v is not None]
        subtotal = vals[-1] if vals else None
        s = sum(r["amount"] or 0 for r in rows)
        if cell_reader and (subtotal is None or abs(subtotal - s) > 50):
            aw = [w for w in sub_line["words"] if lo <= w[0] < hi] or sub_line["words"][-1:]
            t = cell_reader([_box(aw, lo, hi)])[0]
            v = money((t or "").replace(" ", ""))
            if v is not None:
                subtotal = v if subtotal is None or abs(v - s) <= 50 else subtotal
    for r in rows:
        r.pop("_box", None)
        r.pop("_suspect", None)
    return {"rows": rows, "subtotal": subtotal, "cols": c, "ok": True}


def rebuild_schedule(pages_words, sched, cell_reader_for=None):
    """pages_words: [(page, words)] in page order, already limited to this schedule's pages.
    cell_reader_for(page) -> cell_reader for that page (optional).
    Returns {'rows', 'sum', 'pages': {page: {'n', 'sum', 'subtotal', 'subtotal_ok'}}, 'subtotal_mismatch', 'unread'}."""
    out, pages, bad, cols = [], {}, [], None
    for p, ws in pages_words:
        r = rebuild_page(ws, sched, cols, cell_reader_for(p) if cell_reader_for else None)
        if r["cols"]:
            cols = r["cols"]
        s = sum(x["amount"] or 0 for x in r["rows"])
        st = r["subtotal"]
        ok = st is None or abs(st - s) <= 50
        pages[p] = {"n": len(r["rows"]), "sum": s, "subtotal": st, "subtotal_ok": ok}
        if not ok:
            bad.append(p)
        for x in r["rows"]:
            out.append(dict(x, page=p))
    return {"rows": out, "sum": sum(x["amount"] or 0 for x in out), "pages": pages, "subtotal_mismatch": bad,
            "unread": sum(x["amount"] is None for x in out)}


def words_text(words):
    """Plain text of a page from its word boxes (one line per text line), for the summary-box reader."""
    return "\n".join(line_text(l) for l in lines_of(words))


def match_rows(existing, rebuilt):
    """Pair existing OCR rows with rebuilt rows of the same schedule.
    existing: [{'id', 'page', 'amount', 'date', 'ord'}]; rebuilt: [{'page', 'amount', 'date', 'y'}].
    Pass 1 pairs same page + same amount (in reading order); pass 2 pairs the rest on the same page in reading order.
    Returns (pairs [(existing, rebuilt)], unmatched_existing, unmatched_rebuilt)."""
    ex_left = sorted(existing, key=lambda r: (r.get("page") or 0, r.get("ord") or 0))
    rb_left = sorted(rebuilt, key=lambda r: (r["page"], r["y"]))
    pairs = []
    for strict in (True, False):
        nxt_ex = []
        for e in ex_left:
            hit = None
            for i, r in enumerate(rb_left):
                if r["page"] != e.get("page"):
                    continue
                if strict and r["amount"] != e.get("amount"):
                    continue
                hit = i
                break
            if hit is None:
                nxt_ex.append(e)
            else:
                pairs.append((e, rb_left.pop(hit)))
        ex_left = nxt_ex
    return pairs, ex_left, rb_left
