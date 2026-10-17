#!/usr/bin/env python3
"""The Clerk Engine - clerk review of OCR'd campaign money (STAGING ONLY).

Scanned Form 460s are read by OCR into staging D1 campaign_transactions with record_state OCR_UNREVIEWED,
provenance 'ocr', so they never count. This script does what a careful clerk does before they count:

A filing PASSES only if all hold:
  1. Each schedule summary box (A, C, E) is read twice independently and the two reads agree on line 1
     (read A = the ingest's text; read B = fresh tesseract psm 4; read C at 400 dpi breaks a tie: 2 of 3 must agree).
  2. Sum of the filing's OCR rows per schedule = that schedule's itemized line 1 (+/- $0.50).
     A schedule with no box and no rows counts as 0.
  3. It is not a duplicate (same rows as another OCR filing or as ACTIVE e-filed rows) and not superseded by a
     later amendment of the same filer + period.
Anything else is a FLAG. Flags are then worked like a clerk would: an obvious OCR misread (decimal slip,
confusable digit, transposition, a row whose amount was not read, a doubled row) is fixed ONLY when exactly one
fix exists, the fixed value is printed on that page in an OCR read, and the rows then reconcile to the cent.
Every fix is logged before/after (CORRECTED_PASS). Unresolved flags stay OCR_UNREVIEWED.

PASS / CORRECTED_PASS rows go OCR_UNREVIEWED -> ACTIVE (provenance stays 'ocr'), logged per run so a run can be
reverted (--revert RUN_ID). A place's batch is HELD (nothing written for it) on a major discrepancy:
  * a counted Raised/Spent total for a (place, year) that already has counted money moves > 10% or > $250k,
  * a filing > 10x its committee's history (>= 2 other filings) and > $50k,
  * a passing filing whose rows overlap ACTIVE e-filed rows (double counting),
  * any row outside Schedules A/C/E or any Form 496 row in the batch.
Never touches prod. Report: ids and counts only.

Usage:
  python3 review/clerk_review.py --places beverlyhills,culvercity [--max-seconds N] [--dry-run]
         [--text-dir slug=DIR] [--pdf-dir slug=DIR] [--cache-dir DIR] [--report FILE]
  python3 review/clerk_review.py --revert RUN_ID
"""
import argparse, datetime, hashlib, json, os, re, sys, time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import form460 as F  # noqa: E402

TOL = 50                      # $0.50 in cents
ROBOT_V = 2                   # phase 2 robots: a FLAG from an older version is re-read once
MOVE_PCT, MOVE_ABS = 0.10, 25_000_000   # 10% / $250,000 in cents
OOM_MIN = 5_000_000           # $50,000
SCHED = {"460A": "A", "460C": "C", "460E": "E"}
DUP_REASONS = {"duplicate_copy", "superseded_by_amendment", "superseded_by_containing_period", "same_statement_copy", "amendment_order_unknown", "overlapping_periods",
               "possible_duplicate_other_filer", "duplicate_of_efile", "partial_efile_overlap"}

DDL = [
    """CREATE TABLE IF NOT EXISTS clerk_reviews (
  filing_id TEXT PRIMARY KEY, jurisdiction_id TEXT NOT NULL, verdict TEXT NOT NULL, reason TEXT, checks TEXT,
  rows_fp TEXT, applied INTEGER NOT NULL DEFAULT 0, run_id TEXT NOT NULL, reviewed_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS clerk_review_flips (
  run_id TEXT NOT NULL, transaction_id TEXT NOT NULL, filing_id TEXT NOT NULL, from_state TEXT NOT NULL,
  to_state TEXT NOT NULL, flipped_at TEXT NOT NULL, reverted_at TEXT, PRIMARY KEY (run_id, transaction_id))""",
    """CREATE TABLE IF NOT EXISTS clerk_corrections (
  run_id TEXT NOT NULL, transaction_id TEXT NOT NULL, filing_id TEXT NOT NULL, field TEXT NOT NULL,
  before_value TEXT, after_value TEXT, rule TEXT NOT NULL, evidence TEXT, corrected_at TEXT NOT NULL, reverted_at TEXT,
  PRIMARY KEY (run_id, transaction_id, field))""",
    """CREATE TABLE IF NOT EXISTS clerk_review_runs (
  run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, bookmark TEXT, places TEXT, summary TEXT)""",
    "CREATE INDEX IF NOT EXISTS clerk_review_flips_filing ON clerk_review_flips(filing_id)",
]


def log(m):
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {m}', file=sys.stderr, flush=True)


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug_of(jid):
    return jid.replace("jurisdiction-", "", 1)


def page_range(f, n_pages_fn):
    try:
        url = json.loads(f.get("raw_json") or "{}").get("source_url") or ""
    except ValueError:
        url = ""
    m = re.search(r"#pages=(\d+)-(\d+)", url)
    if m:
        return int(m.group(1)), int(m.group(2))
    n = n_pages_fn()
    return (1, n) if n else (None, None)


def row_page(r):
    try:
        return int(json.loads(r.get("raw_json") or "{}").get("page") or 0) or None
    except (ValueError, TypeError):
        return None


FORM_TEXT = re.compile(r"^\W*(of\s*bu\w*|from\b|form\b|sched|name\b|page\b|sub\s*total|amount|date\b|received|statement|cumulative|"
                       r"contributor|per\s*election|to\s*date|calendar|full\s*name|street|address|if\s+an|occupation|employer|"
                       r"code\b|description|payee|total\b)", re.I)


def form_text_name(n):
    """A row 'name' that is really a piece of the printed form (column heading, label) or a few stray glyphs.
    An empty name is not form text: the row counts in totals and simply carries no donor identity."""
    n = (n or "").strip()
    if not n:
        return False
    return len(re.findall(r"[A-Za-z]", n)) < 3 or bool(FORM_TEXT.search(n))


def rows_fp(rows):
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: r["id"]):
        h.update(f"{r['id']}:{r['schedule_code']}:{r['amount_minor']}\n".encode())
    return h.hexdigest()[:16]


def live_rows(rows):
    """Rows a verdict acts on: OCR rows still unreviewed or already flipped by the clerk."""
    return [r for r in rows if r["record_state"] in ("OCR_UNREVIEWED", "ACTIVE")]


# ----------------------------------------------------------------------------- reads
class Deadline(Exception):
    pass


def vote(vals):
    """A figure two reads agree on exactly, and no other figure has as many reads; else None."""
    c = Counter(v for v in vals if v is not None).most_common()
    if not c or c[0][1] < 2 or (len(c) > 1 and c[1][1] == c[0][1]):
        return None
    return c[0][0]


def agreed_box(pages, slug, sha, key, page, sched, deadline):
    """Read the schedule box on `page` with reads A and B (C on disagreement, then D). Returns (l1 or None, detail)."""
    a = F.read_box(pages.read_a(slug, sha, key, page) or "", sched)
    if time.time() > deadline and not pages.cached("B", sha, page):
        raise Deadline()
    b = F.read_box(pages.read("B", slug, sha, key, page, box=True) or "", sched)
    la = a["lines"][0] if a and a.get("lines") else None
    lb = b["lines"][0] if b and b.get("lines") else None
    det = {"page": page, "A": a and a.get("lines"), "B": b and b.get("lines"),
           "arith": bool((a and a["arith"]) or (b and b["arith"]))}
    if la is not None and la == lb:
        return la, det
    if time.time() > deadline and not pages.cached("C", sha, page):
        raise Deadline()
    c = F.read_box(pages.read("C", slug, sha, key, page, box=True) or "", sched)
    lc = c["lines"][0] if c and c.get("lines") else None
    det["C"] = c and c.get("lines")
    v = vote([la, lb, lc])
    if v is not None:
        return v, det
    # robot 3: a third independent engine path (psm 4, 400 dpi, black/white) when the first three do not settle it
    if not getattr(pages, "has_d", True):
        return None, det
    if time.time() > deadline and not pages.cached("D", sha, page):
        raise Deadline()
    d = F.read_box(pages.read("D", slug, sha, key, page, box=True) or "", sched)
    ld = d["lines"][0] if d and d.get("lines") else None
    det["D"] = d and d.get("lines")
    v = vote([la, lb, lc, ld])
    if v is not None:
        det["settled_by"] = "read_D"
    return v, det


def row_ord(r):
    try:
        return int(str(json.loads(r.get("raw_json") or "{}").get("ingest_id", "")).rsplit(":", 1)[-1])
    except (ValueError, TypeError):
        return 0


def schedule_pages(pages, slug, sha, key, p0, p1, sched, srows, deadline):
    """Pages whose printed heading is this schedule (read T), plus pages holding its OCR rows when T has no heading."""
    import tablerows as TR
    rp = {row_page(r) for r in srows if row_page(r)}
    head = re.compile(r"sch\w{0,6}\s*[-_.]?\s*" + sched + r"\b", re.I)
    out = []
    for p in range(p0, p1 + 1):
        first = "\n".join((pages.read_a(slug, sha, key, p) or "").split("\n")[:15])
        if not (p in rp or head.search(first) or pages.cached_words(sha, p)):
            continue                          # not this schedule by the first read: no 400 dpi read needed
        if time.time() > deadline and not pages.cached_words(sha, p):
            raise Deadline()
        ws = pages.words(slug, sha, key, p) or []
        h = TR.page_schedule(ws)
        if h == sched or (h is None and p in rp):
            out.append((p, ws))
    return out


def rebuild(pages, slug, sha, key, p0, p1, sched, srows, deadline):
    """Robot 1: rebuild the schedule's rows from 400 dpi word boxes. Returns the rebuild dict or None."""
    import tablerows as TR
    if not hasattr(pages, "words"):
        return None
    pw = schedule_pages(pages, slug, sha, key, p0, p1, sched, srows, deadline)
    if not pw:
        return None

    def reader_for(p):
        def rd(boxes):
            if time.time() > deadline:
                raise Deadline()
            return pages.cells(slug, sha, key, p, boxes)
        return rd
    rb = TR.rebuild_schedule(pw, sched, reader_for)
    rb["clean"] = bool(rb["rows"]) and not rb["unread"] and not rb["subtotal_mismatch"]
    return rb


def plan_rebuild(f, s, srows, rb, deadline):
    """Corrections that make the stored OCR rows equal the rebuilt rows: amount / name / date fixes on matched rows,
    rejected rows the rebuild does not back, inserted rows the first OCR missed."""
    import tablerows as TR
    sc = {"A": "460A", "C": "460C", "E": "460E"}[s]
    ex = [{"id": r["id"], "page": row_page(r), "amount": r["amount_minor"], "date": r["transaction_date"], "ord": row_ord(r),
           "name": r["counterparty_name_raw"], "row": r} for r in srows]
    pairs, ex_left, rb_left = TR.match_rows(ex, rb["rows"])
    ps, pe = f.get("period_start") or "", f.get("period_end") or ""
    inp = (lambda d: bool(d and ps and pe and ps <= d <= pe))
    out = []
    for e, r in pairs:
        if e["amount"] != r["amount"]:
            out.append({"rule": "rebuild_amount", "transaction_id": e["id"], "field": "amount_minor", "before": e["amount"],
                        "after": r["amount"], "page": r["page"]})
        nm = r["name"] or ""
        if (form_text_name(e["name"]) or not (e["name"] or "").strip()) and len(re.findall(r"[A-Za-z]", nm)) >= 3 and not form_text_name(nm):
            out.append({"rule": "rebuild_name", "transaction_id": e["id"], "field": "counterparty_name_raw", "before": e["name"],
                        "after": nm, "page": r["page"]})
        elif form_text_name(e["name"]):
            out.append({"rule": "clear_form_text_name", "transaction_id": e["id"], "field": "counterparty_name_raw",
                        "before": e["name"], "after": "", "page": r["page"]})
        if s != "E" and r["date"] and r["date"] != e["date"] and inp(r["date"]) and not inp(e["date"]):
            out.append({"rule": "rebuild_date", "transaction_id": e["id"], "field": "transaction_date", "before": e["date"],
                        "after": r["date"], "page": r["page"]})
    for e in ex_left:
        out.append({"rule": "reject_unbacked_row", "transaction_id": e["id"], "field": "record_state", "before": e["row"]["record_state"],
                    "after": "OCR_REJECTED", "page": e["page"]})
    for i, r in enumerate(rb_left):
        nid = "ct_clerk_" + hashlib.sha256(f"{f['id']}:{sc}:{r['page']}:{r['y']}:{r['amount']}".encode()).hexdigest()[:16]
        nm = r["name"] if len(re.findall(r"[A-Za-z]", r["name"] or "")) >= 3 and not form_text_name(r["name"]) else ""
        out.append({"rule": "insert_rebuilt_row", "transaction_id": nid, "field": "inserted", "before": None,
                    "after": json.dumps({"schedule_code": sc, "amount_minor": r["amount"], "name": nm,
                                         "date": r["date"] if (s != "E" and inp(r["date"])) else None, "page": r["page"]}, sort_keys=True),
                    "page": r["page"]})
    return out


def apply_plan(srows, plan):
    """The schedule's rows as they would stand after the plan (for the reconcile check)."""
    by = {r["id"]: dict(r) for r in srows}
    for c in plan:
        if c["field"] == "inserted":
            j = json.loads(c["after"])
            by[c["transaction_id"]] = {"id": c["transaction_id"], "schedule_code": j["schedule_code"], "amount_minor": j["amount_minor"],
                                       "counterparty_name_raw": j["name"], "record_state": "OCR_UNREVIEWED"}
        elif c["field"] == "record_state":
            by.pop(c["transaction_id"], None)
        elif c["transaction_id"] in by:
            by[c["transaction_id"]][c["field"]] = c["after"]
    return list(by.values())


def review_filing(f, rows, pages, deadline):
    """Checks 1 and 2 (+ clerk fixes and robots) for one filing. Returns a result dict."""
    slug, sha, key = slug_of(f["jurisdiction_id"]), f["source_hash"], f["evidence_r2_key"]
    res = {"filing_id": f["id"], "jurisdiction_id": f["jurisdiction_id"], "checks": {"robot_v": ROBOT_V}, "corrections": [],
           "drop": [], "fail": [], "gap": {}}
    if not sha:
        res["fail"].append("no_document")
        return res
    p0, p1 = page_range(f, lambda: pages.pages_of(slug, sha, key))
    if not p0:
        res["fail"].append("no_page_text")
        return res
    texts = {p: pages.read_a(slug, sha, key, p) or "" for p in range(p0, p1 + 1)}
    by_s = defaultdict(list)
    for r in live_rows(rows):
        by_s[SCHED.get(r["schedule_code"], "?")].append(r)
    if "?" in by_s:
        res["fail"].append("rows_outside_A_C_E")
    summ = {}
    for p, t in texts.items():
        if F.is_summary_page(t):
            summ = F.read_summary_page(t)
            break
    for s in ("A", "C", "E"):
        srows = by_s.get(s, [])
        box_pages = pick_box_pages(texts, s)
        if not box_pages and srows:
            # first read missed the box: try the fresh read on the first row page and the page before it
            rp = sorted({row_page(r) for r in srows if row_page(r)})
            for p in ([rp[0] - 1, rp[0]] if rp else []):
                if p0 <= p <= p1:
                    if time.time() > deadline and not pages.cached("B", sha, p):
                        raise Deadline()
                    if F.find_box(pages.read("B", slug, sha, key, p) or "", s) is not None:
                        box_pages.append(p)
        junk = [r["id"] for r in srows if (r["amount_minor"] or 0) > 0 and form_text_name(r["counterparty_name_raw"])]
        chk = {"rows": len(srows), "null_rows": sum(r["amount_minor"] is None for r in srows), "form_text_names": len(junk),
               "rows_sum": sum(r["amount_minor"] or 0 for r in srows)}
        res["checks"][s] = chk
        rb = None
        l1, why = None, None
        if not box_pages:
            if not srows:
                chk["itemized"] = 0
                chk["box"] = "none"
                continue
            # robot 3 (no box in reads A/B): read D and the 400 dpi word boxes of this schedule's pages
            l1, why = find_box_robot(pages, slug, sha, key, p0, p1, s, srows, deadline, chk)
            if l1 is None:
                chk["itemized"] = None
                chk["box"] = "none"
                res["fail"].append(f"{s}:no_summary_box")
                res["gap"][s] = None
                if junk:
                    res["fail"].append(f"{s}:row_name_is_form_text")
                continue
        else:
            reads = []
            for p in box_pages[:3]:
                v, det = agreed_box(pages, slug, sha, key, p, s, deadline)
                reads.append((v, det))
            chk["reads"] = [d for _, d in reads]
            vals = {v for v, _ in reads if v is not None}
            if len(vals) > 1:
                chk["itemized"] = None
                res["fail"].append(f"{s}:multiple_boxes_differ")
                res["gap"][s] = None
                continue
            if vals:
                l1 = vals.pop()
            else:
                # robot 3: the rebuilt rows are the third read - they settle it only when they equal one box read exactly
                rb = rebuild(pages, slug, sha, key, p0, p1, s, srows, deadline)
                box_vals = {x for _, d in reads for k in ("A", "B", "C", "D") for x in ((d.get(k) or [None])[:1])}
                if rb and rb["clean"] and rb["sum"] in box_vals:
                    l1, why = rb["sum"], "rebuilt_rows_match_one_read"
                    chk["settled_by"] = why
                else:
                    chk["itemized"] = None
                    res["fail"].append(f"{s}:summary_reads_disagree")
                    res["gap"][s] = None
                    if junk:
                        res["fail"].append(f"{s}:row_name_is_form_text")
                    continue
        chk["itemized"] = l1
        if s == "A" and summ.get(1) is not None:
            chk["summary_page_line1"] = summ.get(1)
        if s == "E" and summ.get(6) is not None:
            chk["summary_page_line6"] = summ.get(6)
        gap = chk["rows_sum"] - l1
        if abs(gap) <= TOL and not junk:
            chk["ok"] = True
            continue
        plan, how = settle_schedule(f, s, srows, l1, gap, junk, rb, pages, slug, sha, key, p0, p1, deadline)
        if plan is not None:
            chk["ok"] = True
            chk["fixed"] = how
            for c in plan:
                (res["drop"] if c["rule"] == "drop_duplicate_row" else res["corrections"]).append(c)
        else:
            chk["ok"] = False
            if junk:
                res["fail"].append(f"{s}:row_name_is_form_text")
            if abs(gap) > TOL:
                res["fail"].append(f"{s}:rows_do_not_reconcile")
                res["gap"][s] = gap
    return res


def find_box_robot(pages, slug, sha, key, p0, p1, s, srows, deadline, chk):
    """A summary box the first reads did not find: read D and the word-box text of each page of this schedule must
    agree on line 1 (or one of them must equal a clean row rebuild)."""
    import tablerows as TR
    if not hasattr(pages, "words"):
        return None, None
    pw = schedule_pages(pages, slug, sha, key, p0, p1, s, srows, deadline)
    seen = []
    for p, ws in pw:
        t = F.read_box(TR.words_text(ws), s)
        if t is None:
            continue
        if time.time() > deadline and not pages.cached("D", sha, p):
            raise Deadline()
        d = F.read_box(pages.read("D", slug, sha, key, p, box=True) or "", s)
        lt = t["lines"][0] if t.get("lines") else None
        ld = d["lines"][0] if d and d.get("lines") else None
        seen.append({"page": p, "T": t.get("lines"), "D": d and d.get("lines")})
        if lt is not None and lt == ld:
            chk["box_found_by"] = "read_T+D"
            chk["reads"] = seen
            return lt, "read_T+D"
    if seen:
        rb = rebuild(pages, slug, sha, key, p0, p1, s, srows, deadline)
        vals = {x for r in seen for k in ("T", "D") for x in ((r.get(k) or [None])[:1])}
        if rb and rb["clean"] and rb["sum"] in vals:
            chk["box_found_by"] = "rebuilt_rows_match_one_read"
            chk["reads"] = seen
            return rb["sum"], "rebuilt_rows_match_one_read"
    chk["reads"] = seen
    return None, None


def settle_schedule(f, s, srows, l1, gap, junk, rb, pages, slug, sha, key, p0, p1, deadline):
    """Robots 1, 2 and the clerk fixes, in that order. Returns (plan, how) or (None, None).
    A plan only stands when the schedule's rows after it add up to line 1 within $0.50."""
    def ok(plan):
        after = apply_plan(srows, plan)
        return abs(sum(r.get("amount_minor") or 0 for r in after) - l1) <= TOL

    # robot 1: rebuilt rows from the 400 dpi table read
    if rb is None:
        rb = rebuild(pages, slug, sha, key, p0, p1, s, srows, deadline)
    if rb and rb["clean"] and abs(rb["sum"] - l1) <= TOL:
        plan = plan_rebuild(f, s, srows, rb, deadline)
        if ok(plan):
            return plan, "rebuilt_rows"
    # robot 2: rows named with printed form text - drop them if the rest reconciles; keep them (name cleared)
    # when they are needed for the schedule to reconcile
    if junk:
        heads = [i for i in junk if FORM_TEXT.search(next(r["counterparty_name_raw"] for r in srows if r["id"] == i) or "")]
        for subset, rule in ((heads, "drop_form_text_row"), (junk, "drop_form_text_row")):
            if not subset:
                continue
            plan = [{"rule": rule, "transaction_id": i, "field": "record_state", "before": "OCR_UNREVIEWED", "after": "OCR_REJECTED",
                     "page": next(row_page(r) for r in srows if r["id"] == i)} for i in subset]
            if ok(plan):
                return plan, rule
        clear = [{"rule": "clear_form_text_name", "transaction_id": i, "field": "counterparty_name_raw",
                  "before": next(r["counterparty_name_raw"] for r in srows if r["id"] == i), "after": "",
                  "page": next(row_page(r) for r in srows if r["id"] == i)} for i in junk]
        if abs(gap) <= TOL:
            return clear, "clear_form_text_name"
        fix = clerk_fix(srows, gap, pages, slug, sha, key, deadline)
        if fix and fix["transaction_id"] and ok([fix]):
            return [fix] + [c for c in clear if not (fix["rule"] == "drop_duplicate_row" and c["transaction_id"] == fix["transaction_id"])], fix["rule"] + "+clear_form_text_name"
        return None, None
    # the clerk's single evidence-backed fix
    if abs(gap) > TOL:
        fix = clerk_fix(srows, gap, pages, slug, sha, key, deadline)
        if fix:
            return [fix], fix["rule"]
    return None, None


def pick_box_pages(texts, s):
    """Pages holding this schedule's summary box. Some filers photocopy the first sheet (box included) for every
    page, so only boxes that carry a non-zero amount in the first read count; if none do, the first box page."""
    pages_ = [p for p, t in sorted(texts.items()) if F.find_box(t, s) is not None]
    filled = []
    for p in pages_:
        r = F.read_box(texts[p], s)
        if r and r.get("lines") and any(v for v in r["lines"] if v):
            filled.append(p)
    return filled[:3] if filled else pages_[:1]


def prefetch(todo, filings, pages, deadline, workers=None, robots_for=None):
    """Run read B for every summary-box page of the filings to review (and read T for the schedule pages of
    filings flagged before, {fid: rows}), in parallel, until the deadline."""
    from concurrent.futures import ThreadPoolExecutor
    jobs = []
    for fid in todo:
        f = filings[fid]
        slug, sha, key = slug_of(f["jurisdiction_id"]), f["source_hash"], f["evidence_r2_key"]
        if not sha:
            continue
        p0, p1 = page_range(f, lambda: pages.pages_of(slug, sha, key))
        if not p0:
            continue
        texts = {p: pages.read_a(slug, sha, key, p) or "" for p in range(p0, p1 + 1)}
        for s in "ACE":
            for p in pick_box_pages(texts, s):
                if not pages.cached("B", sha, p):
                    jobs.append(("B", slug, sha, key, p))
        if robots_for and fid in robots_for and hasattr(pages, "words"):
            # a filing flagged before: its schedule pages get the 400 dpi word-box read the robots need
            rp = {row_page(r) for r in robots_for[fid]}
            head = re.compile(r"sch\w{0,6}\s*[-_.]?\s*[ACE]\b", re.I)
            for p, t in texts.items():
                if (p in rp or head.search("\n".join(t.split("\n")[:15]))) and not pages.cached_words(sha, p):
                    jobs.append(("T", slug, sha, key, p))

    jobs = sorted(set(jobs))

    def one(j):
        if time.time() > deadline:
            return
        try:
            if j[0] == "T":
                pages.words(*j[1:])
            else:
                pages.read("B", *j[1:], box=True)
        except Exception as e:                # a bad page never stops the prefetch; the review re-reads it
            log(f"prefetch {j[0]} {j[2][:8]} p{j[4]}: {type(e).__name__}")
    with ThreadPoolExecutor(max_workers=workers or max(1, os.cpu_count() or 1)) as ex:
        list(ex.map(one, jobs))


def clerk_fix(srows, gap, pages, slug, sha, key, deadline):
    """Exactly one evidence-backed fix that makes rows == itemized to the cent, else None."""
    cands = []

    def printed(p, v):
        """v is printed on page p: first read, else the fresh read."""
        if F.text_has_amount(pages.read_a(slug, sha, key, p) or "", v):
            return True
        if time.time() > deadline and not pages.cached("B", sha, p):
            raise Deadline()
        return F.text_has_amount(pages.read("B", slug, sha, key, p) or "", v)

    # a doubled row: same page, name, date, amount; removing one copy closes the gap exactly
    seen = Counter((row_page(r), r["counterparty_name_raw"], r["transaction_date"], r["amount_minor"]) for r in srows)
    for r in srows:
        k = (row_page(r), r["counterparty_name_raw"], r["transaction_date"], r["amount_minor"])
        if r["amount_minor"] == gap and seen[k] > 1:
            cands.append({"rule": "drop_duplicate_row", "transaction_id": r["id"], "field": "record_state",
                          "before": r["record_state"], "after": "DUPLICATE", "page": row_page(r)})
            break
    # a row whose amount was not read: the missing amount is printed on its page
    if gap < 0:
        for r in srows:
            if r["amount_minor"] is None and row_page(r):
                if printed(row_page(r), -gap):
                    cands.append({"rule": "fill_unread_amount", "transaction_id": r["id"], "field": "amount_minor",
                                  "before": None, "after": -gap, "page": row_page(r)})
    # a misread amount: decimal slip / confusable digit / transposition / dropped or doubled digit
    for r in srows:
        a = r["amount_minor"]
        if a is None or not row_page(r):
            continue
        v = a - gap
        if v > 0 and v in F.misread_variants(a):
            if printed(row_page(r), v):
                cands.append({"rule": "fix_misread_amount", "transaction_id": r["id"], "field": "amount_minor",
                              "before": a, "after": v, "page": row_page(r)})
    if len(cands) == 1:
        return cands[0]
    return None


# ----------------------------------------------------------------------------- duplicates / supersession
def name_key(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


def near_period(pa, pb, days=3):
    """Same reporting period, allowing an OCR misread of a day or three at either end."""
    if pa == pb:
        return True
    if not (all(pa) and all(pb)):
        return False
    try:
        d = [abs((datetime.date.fromisoformat(x) - datetime.date.fromisoformat(y)).days) for x, y in zip(pa, pb)]
    except ValueError:
        return False
    return max(d) <= days


def dup_groups(filings, rows_by, efile_keys, pending=None):
    """{filing_id: (reason, other)} for OCR filings that are duplicates or superseded.

    FPPC rules, applied per committee (same filer, or same rows):
      * same period (ends within 3 days = an OCR date misread): the highest amendment, then the latest filing date wins;
      * a tie with identical rows is a re-released copy (keep one); a tie with different rows is either two scans of one
        statement or original + amendment - when `pending` is given these are returned there for the copy check
        (resolve_copies: identical summary boxes = one statement), else flagged amendment_order_unknown;
      * different periods where one fully contains the other and every amount of the contained one is in the container:
        the contained statement is superseded (counting both would double count);
      * any other overlap of one committee's periods stays flagged (overlapping_periods).
    """
    out = {}
    sig, amt = {}, {}
    for fid, f in filings.items():
        lr = [r for r in live_rows(rows_by.get(fid, [])) if r["amount_minor"] is not None]
        sig[fid] = Counter((r["schedule_code"], r["amount_minor"], r["transaction_date"]) for r in lr if r["transaction_date"])
        amt[fid] = Counter((r["schedule_code"], r["amount_minor"]) for r in lr)
    by_j = defaultdict(list)
    for fid, f in filings.items():
        if live_rows(rows_by.get(fid, [])):   # filings whose rows the ingest already superseded are out
            by_j[f["jurisdiction_id"]].append(fid)

    def per(f):
        return (f["period_start"], f["period_end"])

    def pages_key(f):
        try:
            u = json.loads(f.get("raw_json") or "{}").get("source_url") or ""
        except ValueError:
            u = ""
        m = re.search(r"#pages=(\d+-\d+)", u)
        return (f.get("source_hash"), m.group(1) if m else "all") if f.get("source_hash") else None

    for j, ids in by_j.items():
        ids.sort()
        groups = {}

        def find(x):
            while groups.get(x, x) != x:
                x = groups[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                groups[max(ra, rb)] = min(ra, rb)

        cross = defaultdict(list)
        for i, a in enumerate(ids):
            fa = filings[a]
            for b in ids[i + 1:]:
                fb = filings[b]
                pa, pb = per(fa), per(fb)
                overlap_period = all(pa) and all(pb) and pa[0] <= pb[1] and pb[0] <= pa[1]
                same_filer = bool(fa["filer_identity_id"] and fa["filer_identity_id"] == fb["filer_identity_id"]) or \
                    bool(name_key(fa["filer_name_raw"]) and name_key(fa["filer_name_raw"]) == name_key(fb["filer_name_raw"]))
                unread_filer = not name_key(fa["filer_name_raw"]) or not name_key(fb["filer_name_raw"])
                sa, sb = sig[a], sig[b]
                small = min(sum(sa.values()), sum(sb.values()))
                ov = sum((sa & sb).values()) / small if small else 0.0
                identical = small >= 3 and sa == sb
                same_doc = pages_key(fa) is not None and pages_key(fa) == pages_key(fb)
                if same_filer and (overlap_period or (small >= 5 and ov >= 0.5)):
                    union(a, b)
                elif identical or same_doc:
                    union(a, b)
                elif unread_filer and near_period(pa, pb) and small >= 5 and ov >= 0.5:
                    union(a, b)               # filer name not read on one copy: same period + same rows = same committee
                elif small >= 5 and ov >= 0.5 and (overlap_period or not (all(pa) and all(pb))):
                    # different (or unread) filer, same rows: a copy filed under another name, or slate donors
                    cross[a].append(b)
                    cross[b].append(a)
        members = defaultdict(list)
        for fid in ids:
            members[find(fid)].append(fid)
        for root, ms in members.items():
            if len(ms) < 2:
                continue
            # 1. cluster the committee's statements by reporting period
            clusters = []
            for m in sorted(ms):
                for c in clusters:
                    if near_period(per(filings[m]), per(filings[c[0]])) or (sig[m] and sig[m] == sig[c[0]]):
                        c.append(m)
                        break
                else:
                    clusters.append([m])
            heads = []

            def rank(fid):
                f = filings[fid]
                return (f["amendment_number"] or 0, f["filing_date"] or "")
            for c in clusters:
                if len(c) == 1:
                    heads.append((c[0], [c[0]]))
                    continue
                best = max(c, key=rank)
                ties = [m for m in c if rank(m) == rank(best)]
                for m in c:
                    if m not in ties:
                        out[m] = ("superseded_by_amendment", best)
                if len(ties) > 1:
                    if all(sig[m] == sig[ties[0]] for m in ties) and sig[ties[0]]:
                        keep = min(ties)      # re-released copy of the same statement: keep one, deterministic
                        for m in ties:
                            if m != keep:
                                out[m] = ("duplicate_copy", keep)
                        heads.append((keep, [keep]))
                    elif pending is not None:
                        for m in ties:        # two scans of one statement, or original + amendment: the copy check decides
                            pending[m] = sorted(ties)
                        heads.append((min(ties), sorted(ties)))
                    else:
                        for m in ties:        # original + amendment, can't tell which is newer: a clerk decides
                            out[m] = ("amendment_order_unknown", ",".join(x for x in ties if x != m))
                else:
                    heads.append((best, [best]))
            # 2. statements of different periods that overlap
            for i, (ha, ga) in enumerate(heads):
                for hb, gb in heads[i + 1:]:
                    pa, pb = per(filings[ha]), per(filings[hb])
                    if not (all(pa) and all(pb) and pa[0] <= pb[1] and pb[0] <= pa[1]):
                        continue
                    done = False
                    for outer, inner, go, gi in ((ha, hb, ga, gb), (hb, ha, gb, ga)):
                        po, pi = per(filings[outer]), per(filings[inner])
                        if po[0] <= pi[0] and pi[1] <= po[1] and po != pi and len(go) == 1 and len(gi) == 1:
                            if amt[inner] and not (amt[inner] - amt[outer]):
                                out[inner] = ("superseded_by_containing_period", outer)
                                done = True
                                break
                    if not done:
                        for g, other in ((ga, hb), (gb, ha)):
                            for m in g:
                                if m not in out:
                                    out[m] = ("overlapping_periods", other)
                                    if pending is not None:
                                        pending.pop(m, None)
        for a, bs in cross.items():
            if a not in out and a not in (pending or {}):
                out[a] = ("possible_duplicate_other_filer", ",".join(sorted(bs)))
    # same rows as ACTIVE e-filed rows in the same place
    for fid, f in filings.items():
        if fid in out or fid in (pending or {}):
            continue
        rs = [r for r in live_rows(rows_by.get(fid, [])) if r["amount_minor"] is not None]
        if not rs:
            continue
        keys = efile_keys.get(f["jurisdiction_id"], set())
        hit = sum((r["schedule_code"], r["amount_minor"], r["transaction_date"]) in keys for r in rs if r["transaction_date"])
        if hit >= 3 and hit / len(rs) >= 0.5:
            out[fid] = ("duplicate_of_efile", f"{hit}/{len(rs)} rows")
        elif hit >= 2 and hit / len(rs) >= 0.2:
            out[fid] = ("partial_efile_overlap", f"{hit}/{len(rs)} rows")
    return out


def effective_rows(rows, r, f):
    """The filing's live rows as they stand once its verdict's corrections are applied (rejected / doubled rows out,
    rebuilt rows in, amounts, names and dates fixed)."""
    by = {t["id"]: dict(t) for t in live_rows(rows)}
    tmpl = next(iter(by.values()), {})
    for c in r.get("drop", []):
        by.pop(c["transaction_id"], None)
    for c in r.get("corrections", []):
        if c["field"] == "inserted":
            j = json.loads(c["after"])
            by[c["transaction_id"]] = {"id": c["transaction_id"], "jurisdiction_id": f["jurisdiction_id"], "filing_id": f["id"],
                                       "schedule_code": j["schedule_code"], "amount_minor": j["amount_minor"],
                                       "transaction_date": j["date"], "counterparty_name_raw": j["name"],
                                       "record_state": "OCR_UNREVIEWED", "provenance": "ocr",
                                       "cycle_year": tmpl.get("cycle_year") or f.get("cycle_year")}
        elif c["field"] == "record_state":
            by.pop(c["transaction_id"], None)
        elif c["transaction_id"] in by:
            by[c["transaction_id"]][c["field"]] = c["after"]
    return list(by.values())


KIND = {"460A": ("CONTRIBUTION", 0), "460C": ("NONMONETARY", 1), "460E": ("EXPENDITURE", 0)}


def correction_sql(c, fid, rows, f, run_id, ts):
    """SQL for one logged correction. Every statement is guarded on the value it replaces."""
    import d1 as d1mod
    lit = d1mod.lit
    tid = lit(c["transaction_id"])
    if c["field"] == "amount_minor":
        return [f"UPDATE campaign_transactions SET amount_minor = {lit(c['after'])}, updated_at = {lit(ts)} WHERE id = {tid} AND amount_minor IS {lit(c['before'])} AND provenance = 'ocr'"]
    if c["field"] in ("counterparty_name_raw", "transaction_date"):
        return [f"UPDATE campaign_transactions SET {c['field']} = {lit(c['after'])}, updated_at = {lit(ts)} WHERE id = {tid} AND {c['field']} IS {lit(c['before'])} AND record_state = 'OCR_UNREVIEWED' AND provenance = 'ocr'"]
    if c["field"] == "record_state":
        return [f"UPDATE campaign_transactions SET record_state = {lit(c['after'])}, updated_at = {lit(ts)} WHERE id = {tid} AND record_state = 'OCR_UNREVIEWED' AND provenance = 'ocr'"]
    if c["field"] == "inserted":
        j = json.loads(c["after"])
        sc = j["schedule_code"]
        same = [t for t in rows if t["schedule_code"] == sc]
        t = (same or rows)[0]
        filer = None
        if t["schedule_code"] in ("460A", "460C"):
            filer = t.get("to_identity_id")
        else:
            filer = t.get("from_identity_id")
        to_id, from_id = (filer, None) if sc in ("460A", "460C") else (None, filer)
        kind, nonmon = KIND[sc]
        doc = t.get("source_document_url") or ""
        raw = json.dumps({"clerk_run": run_id, "page": j["page"], "confidence": "clerk_rebuild",
                          "source_url": f"{doc}#page={j['page']}" if doc else None}, separators=(",", ":"))
        cols = ("id, jurisdiction_id, source_system_id, filing_id, election_id, source_key, source_transaction_id, transaction_kind, "
                "schedule_code, transaction_date, amount_minor, currency, from_identity_id, to_identity_id, counterparty_name_raw, "
                "recipient_name_raw, is_nonmonetary, is_refund, cycle_year, source_row_hash, record_state, raw_json, first_seen_at, "
                "last_seen_at, created_at, updated_at, source_document_url, provenance")
        vals = [c["transaction_id"], f["jurisdiction_id"], t.get("source_system_id"), fid, t.get("election_id"), "clerk:" + c["transaction_id"],
                c["transaction_id"], kind, sc, j["date"], j["amount_minor"], "USD", from_id, to_id, j["name"], t.get("recipient_name_raw"),
                nonmon, 0, t.get("cycle_year"), t.get("source_row_hash"), "OCR_UNREVIEWED", raw, ts, ts, ts, ts, doc or None, "ocr"]
        return [f"INSERT OR IGNORE INTO campaign_transactions ({cols}) VALUES ({', '.join(lit(v) for v in vals)})"]
    return []


def summary_triple(fid, results, prior):
    r = results.get(fid)
    ch = r["checks"] if r else json.loads((prior.get(fid) or {}).get("checks") or "{}")
    return tuple(ch.get(s, {}).get("itemized") for s in "ACE")


def verdict_of(fid, results, prior):
    r = results.get(fid)
    return r["verdict"] if r else (prior.get(fid) or {}).get("verdict")


def resolve_copies(pending, results, prior):
    """Robot 4, copy check: statements of one committee and period that tie on amendment number and filing date.
    When every member's summary boxes (A, C, E line 1, each agreed by two reads) are identical they are copies of one
    statement: one counts (the lowest id that passes) and the rest are same_statement_copy. Anything else stays
    amendment_order_unknown for a clerk."""
    seen = set()
    for fid, ms in sorted(pending.items()):
        key = tuple(ms)
        if key in seen:
            continue
        seen.add(key)
        trip = {m: summary_triple(m, results, prior) for m in ms}
        same = all(None not in t for t in trip.values()) and len(set(trip.values())) == 1
        if same:
            passing = [m for m in ms if verdict_of(m, results, prior) in ("PASS", "CORRECTED_PASS")]
            keep = min(passing) if passing else min(ms)
            for m in ms:
                if m == keep or m not in results:
                    continue
                r = results[m]
                r.update(verdict="FLAG", corrections=[], drop=[], fail=["same_statement_copy"] + [x for x in r["fail"] if x != "same_statement_copy"])
                r["checks"]["duplicate"] = {"reason": "same_statement_copy", "other": keep}
            if keep in results:
                results[keep]["checks"]["copies"] = [m for m in ms if m != keep]
        else:
            for m in ms:
                if m not in results:
                    continue
                r = results[m]
                r.update(verdict="FLAG", fail=["amendment_order_unknown"] + r["fail"])
                r["checks"]["duplicate"] = {"reason": "amendment_order_unknown", "other": ",".join(x for x in ms if x != m),
                                            "summaries": {k: list(v) for k, v in trip.items()}}


# ----------------------------------------------------------------------------- main
def load(d1, places):
    jids = ",".join(F_lit(f"jurisdiction-{p}") for p in places)
    tx = d1.all(f"""SELECT id, jurisdiction_id, filing_id, schedule_code, transaction_kind, amount_minor, transaction_date,
        record_state, provenance, counterparty_name_raw, cycle_year, raw_json, to_identity_id, from_identity_id, recipient_name_raw,
        source_document_url, source_row_hash, election_id, source_system_id FROM campaign_transactions
        WHERE jurisdiction_id IN ({jids}) AND provenance = 'ocr' ORDER BY id""")
    fids = sorted({t["filing_id"] for t in tx})
    filings = {}
    for i in range(0, len(fids), 80):
        chunk = ",".join(F_lit(x) for x in fids[i:i + 80])
        for f in d1.q(f"""SELECT id, jurisdiction_id, filer_identity_id, filer_name_raw, form_number, amendment_number,
            filing_date, period_start, period_end, cycle_year, record_state, evidence_r2_key, source_hash, raw_json
            FROM campaign_filings WHERE id IN ({chunk})"""):
            filings[f["id"]] = f
    ef = d1.all(f"""SELECT t.id, t.jurisdiction_id, t.filing_id, t.schedule_code, t.amount_minor, t.transaction_date, t.cycle_year,
        COALESCE(t.provenance, '') AS provenance, f.filer_identity_id
        FROM campaign_transactions t LEFT JOIN campaign_filings f ON f.id = t.filing_id
        WHERE t.jurisdiction_id IN ({jids}) AND t.record_state = 'ACTIVE' AND t.schedule_code IN ('460A','460C','460E') ORDER BY t.id""")
    try:
        prior = {r["filing_id"]: r for r in d1.all(f"SELECT * FROM clerk_reviews WHERE jurisdiction_id IN ({jids}) ORDER BY filing_id")}
    except RuntimeError:
        prior = {}
    return tx, filings, ef, prior


def F_lit(v):
    import d1 as _d
    return _d.lit(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--places", default="beverlyhills,culvercity")
    ap.add_argument("--max-seconds", type=int, default=3000)
    ap.add_argument("--dry-run", action="store_true", help="review and report; write nothing")
    ap.add_argument("--text-dir", action="append", default=[], help="slug=DIR (derived/text)")
    ap.add_argument("--pdf-dir", action="append", default=[], help="slug=DIR (raw/objects)")
    ap.add_argument("--cache-dir", default=os.path.join(HERE, "..", ".cache", "clerk-review"))
    ap.add_argument("--report", default=None)
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID") and f"gh-{os.environ['GITHUB_RUN_ID']}")
    ap.add_argument("--revert", default=None, help="RUN_ID to revert")
    ap.add_argument("--no-r2", action="store_true")
    args = ap.parse_args()
    import d1 as d1mod
    from pages import Pages
    db = d1mod.D1()
    db.q("SELECT 1")                                      # auth preflight
    if args.revert:
        return revert(db, args.revert)
    started = now()
    run_id = args.run_id or "local-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    deadline = time.time() + args.max_seconds
    places = [p.strip() for p in args.places.split(",") if p.strip()]
    pages = Pages(args.cache_dir, dict(x.split("=", 1) for x in args.text_dir), dict(x.split("=", 1) for x in args.pdf_dir), r2=not args.no_r2)
    tx, filings, ef, prior = load(db, places)
    rows_by = defaultdict(list)
    for t in tx:
        rows_by[t["filing_id"]].append(t)
    efile_keys = defaultdict(set)
    for r in ef:
        if r["provenance"] != "ocr":
            efile_keys[r["jurisdiction_id"]].add((r["schedule_code"], r["amount_minor"], r["transaction_date"]))
    pending = {}
    dups = dup_groups(filings, rows_by, efile_keys, pending=pending)

    todo, reapply = [], []
    for fid, f in sorted(filings.items()):
        lr = live_rows(rows_by[fid])
        if not any(r["record_state"] == "OCR_UNREVIEWED" for r in lr):
            continue
        p = prior.get(fid)
        if p and p["rows_fp"] == rows_fp(lr) and p["verdict"] in ("PASS", "CORRECTED_PASS"):
            reapply.append(fid)              # verdict stands; rows were reset by a re-promote or never applied
        elif p and p["rows_fp"] == rows_fp(lr) and p["verdict"] == "FLAG" and not (p["reason"] or "").startswith("reverted"):
            was_dup = (p["reason"] or "").split(";")[0] in DUP_REASONS
            try:
                pv = json.loads(p["checks"] or "{}").get("robot_v", 1)
            except ValueError:
                pv = 1
            if was_dup == (fid in dups or fid in pending) and pv >= ROBOT_V:
                continue                     # same rows, same duplicate status, same robots: the flag stands
            todo.append(fid)
        else:
            todo.append(fid)

    log(f'loaded {len(filings)} filings; todo {len(todo)} reapply {len(reapply)}')
    reflag = {f: live_rows(rows_by[f]) for f in todo if f not in dups and prior.get(f) and prior[f]["verdict"] == "FLAG"}
    prefetch([f for f in todo if f not in dups], filings, pages, deadline, robots_for=reflag)
    log(f'prefetch done ocr={pages.stats}')
    results, deferred = {}, []
    for fid in todo:
        f = filings[fid]
        if fid in dups:                       # a duplicate / superseded / overlapping filing never counts: no reads needed
            results[fid] = {"filing_id": fid, "jurisdiction_id": f["jurisdiction_id"], "verdict": "FLAG", "corrections": [], "drop": [],
                            "fail": [dups[fid][0]], "gap": {}, "checks": {"robot_v": ROBOT_V, "duplicate": {"reason": dups[fid][0], "other": dups[fid][1]}}}
            continue
        try:
            r = review_filing(f, rows_by[fid], pages, deadline)
        except Deadline:
            deferred.append(fid)
            continue
        except Exception as e:                # one bad document never stops the run
            r = {"filing_id": fid, "jurisdiction_id": f["jurisdiction_id"], "checks": {}, "corrections": [], "drop": [],
                 "fail": [f"error:{type(e).__name__}"], "gap": {}}
        if fid in dups:
            r["fail"].insert(0, f"{dups[fid][0]}")
            r["checks"]["duplicate"] = {"reason": dups[fid][0], "other": dups[fid][1]}
        r["verdict"] = "FLAG" if r["fail"] else ("CORRECTED_PASS" if (r["corrections"] or r["drop"]) else "PASS")
        results[fid] = r
    resolve_copies(pending, results, prior)
    for fid in reapply:
        p = prior[fid]
        results[fid] = {"filing_id": fid, "jurisdiction_id": filings[fid]["jurisdiction_id"], "verdict": p["verdict"],
                        "checks": json.loads(p["checks"] or "{}"), "corrections": [], "drop": [], "fail": [], "gap": {}, "reapply": True}
        if fid in dups:                       # a newer duplicate/amendment showed up since: re-flag
            results[fid].update(verdict="FLAG", fail=[dups[fid][0]], reapply=False)
    for fid, r in results.items():
        r.setdefault("inserts", [])

    # ---- per place money + major-discrepancy checks
    base = defaultdict(int)                 # (jid, year, sched) -> counted cents now
    for r in ef:
        base[(r["jurisdiction_id"], r["cycle_year"], r["schedule_code"])] += r["amount_minor"] or 0
    # fixes only exist for filings that pass; a flagged filing's candidate fixes are never applied
    eff = {fid: effective_rows(rows_by[fid], r, filings[fid]) for fid, r in results.items() if r["verdict"] != "FLAG"}
    place = {p: {"reviewed": 0, "PASS": 0, "CORRECTED_PASS": 0, "FLAG": 0, "deferred": 0, "reapplied": 0,
                 "flip_rows": 0, "raised_minor": 0, "spent_minor": 0, "nonmonetary_minor": 0, "held": False,
                 "hold_reasons": []} for p in places}
    flips = defaultdict(list)
    added = defaultdict(int)
    hist = defaultdict(list)                # committee -> per-filing A totals (e-filed + OCR)
    for r in ef:
        if r["schedule_code"] == "460A" and r["filer_identity_id"]:
            hist[r["filer_identity_id"]].append((r["filing_id"], r["amount_minor"] or 0))
    for fid in deferred:
        place[slug_of(filings[fid]["jurisdiction_id"])]["deferred"] += 1
    for fid, r in results.items():
        pl = place[slug_of(r["jurisdiction_id"])]
        pl["reviewed"] += 0 if r.get("reapply") else 1
        pl["reapplied"] += 1 if r.get("reapply") else 0
        pl[r["verdict"]] += 1
        if r["verdict"] == "FLAG":
            continue
        for t in eff[fid]:
            if t["record_state"] != "OCR_UNREVIEWED":
                continue
            amt = t["amount_minor"]
            if amt is None or amt <= 0:
                continue                      # a row with no (or a zero) amount stays unreviewed
            flips[fid].append(t["id"])
            k = {"460A": "raised_minor", "460E": "spent_minor", "460C": "nonmonetary_minor"}.get(t["schedule_code"])
            if not k:
                pl["hold_reasons"].append(f"row outside A/C/E in {fid}")
                continue
            pl[k] += amt
            pl["flip_rows"] += 1
            added[(r["jurisdiction_id"], t["cycle_year"], t["schedule_code"])] += amt
        f = filings[fid]
        if (f.get("form_number") or "") == "496":
            pl["hold_reasons"].append(f"form 496 filing {fid} in batch")
        if r["checks"].get("duplicate", {}).get("reason") == "partial_efile_overlap":
            pl["hold_reasons"].append(f"passing filing {fid} overlaps ACTIVE e-filed rows")
        keys = efile_keys.get(f["jurisdiction_id"], set())
        er = [t for t in eff[fid] if t["amount_minor"] and t.get("transaction_date")]
        hit = sum((t["schedule_code"], t["amount_minor"], t["transaction_date"]) in keys for t in er)
        if er and hit >= 2 and hit / len(er) >= 0.2:
            pl["hold_reasons"].append(f"passing filing {fid} rows match ACTIVE e-filed rows ({hit}/{len(er)})")
        a_tot = sum(t["amount_minor"] or 0 for t in eff[fid] if t["schedule_code"] == "460A")
        h = defaultdict(int)
        for ofid, amt in hist.get(f["filer_identity_id"], []):
            if ofid != fid:
                h[ofid] += amt
        if len(h) >= 2 and a_tot > OOM_MIN and a_tot > 10 * max(h.values()):
            pl["hold_reasons"].append(f"filing {fid} raised {a_tot} > 10x committee history max {max(h.values())}")
    moves = []
    for (jid, yr, sc), amt in sorted(added.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0, kv[0][2])):
        if sc not in ("460A", "460E"):
            continue
        b = base.get((jid, yr, sc), 0)
        moves.append({"place": slug_of(jid), "year": yr, "schedule": sc, "counted_before_minor": b, "added_minor": amt})
        if b > 0 and (amt > MOVE_PCT * b or amt > MOVE_ABS):
            place[slug_of(jid)]["hold_reasons"].append(f"{sc} {yr} counted total moves {amt} on base {b}")
    # the known Culver City 2022 Form 496 possible double count must not be touched
    f496_2022 = [fid for fid in flips if filings[fid].get("form_number") == "496" and filings[fid].get("cycle_year") == 2022]
    for p in places:
        place[p]["held"] = bool(place[p]["hold_reasons"])

    flags = []
    for fid, r in sorted(results.items()):
        if r["verdict"] != "FLAG":
            continue
        reason = r["fail"][0] if r["fail"] else "unknown"
        gaps = {s: g for s, g in r["gap"].items()}
        flags.append({"filing_id": fid, "place": slug_of(r["jurisdiction_id"]), "reason": ";".join(r["fail"]),
                      "gap_minor": gaps, "auto_excluded": reason.startswith(("duplicate_copy", "superseded", "duplicate_of_efile", "same_statement_copy"))})
    # the public report carries ids, rules and amounts only: names, dates and rebuilt-row details stay in D1
    corrections = [{"filing_id": fid, "transaction_id": c["transaction_id"], "rule": c["rule"], "field": c["field"], "page": c.get("page"),
                    **({"before": c["before"], "after": c["after"]} if c["field"] in ("amount_minor", "record_state") else {}),
                    **({"after_amount_minor": json.loads(c["after"])["amount_minor"]} if c["field"] == "inserted" else {})}
                   for fid, r in results.items() if r["verdict"] == "CORRECTED_PASS" for c in r["corrections"] + r["drop"]]

    passed = [{"filing_id": fid, "place": slug_of(r["jurisdiction_id"]), "verdict": r["verdict"], "reapply": bool(r.get("reapply")),
               "rows_flipped": len(flips.get(fid, [])),
               **{f"{s}_itemized_minor": r["checks"].get(s, {}).get("itemized") for s in "ACE"}}
              for fid, r in sorted(results.items()) if r["verdict"] != "FLAG"]
    report = {"run_id": run_id, "passed": passed, "started_at": started, "dry_run": args.dry_run, "places": place,
              "deferred_filings": deferred, "year_moves": moves, "f496_2022_touched": f496_2022,
              "flags": flags, "corrections": corrections, "ocr": pages.stats}

    # ---- writes
    if not args.dry_run:
        bookmark = db.bookmark()
        report["bookmark_before"] = bookmark
        db.exec_many(DDL)
        lit, ts = d1mod.lit, now()
        st = [f"INSERT OR REPLACE INTO clerk_review_runs (run_id, started_at, bookmark, places) VALUES ({lit(run_id)}, {lit(started)}, {lit(bookmark)}, {lit(','.join(places))})"]
        for fid, r in results.items():
            p = place[slug_of(r["jurisdiction_id"])]
            apply = r["verdict"] != "FLAG" and not p["held"]
            if apply:
                for c in r["corrections"]:
                    st += correction_sql(c, fid, rows_by[fid], filings[fid], run_id, ts)
                for c in r["drop"]:
                    st.append(f"UPDATE campaign_transactions SET record_state = 'DUPLICATE', updated_at = {lit(ts)} WHERE id = {lit(c['transaction_id'])} AND record_state = 'OCR_UNREVIEWED' AND provenance = 'ocr'")
                for c in r["corrections"] + r["drop"]:
                    st.append(f"INSERT OR REPLACE INTO clerk_corrections (run_id, transaction_id, filing_id, field, before_value, after_value, rule, evidence, corrected_at) VALUES ({lit(run_id)}, {lit(c['transaction_id'])}, {lit(fid)}, {lit(c['field'])}, {lit(c['before'])}, {lit(c['after'])}, {lit(c['rule'])}, {lit('page ' + str(c.get('page')))}, {lit(ts)})")
                ids = flips.get(fid, [])
                for i in range(0, len(ids), 200):
                    chunk = ",".join(lit(x) for x in ids[i:i + 200])
                    st.append(f"INSERT OR IGNORE INTO clerk_review_flips (run_id, transaction_id, filing_id, from_state, to_state, flipped_at) SELECT {lit(run_id)}, id, filing_id, record_state, 'ACTIVE', {lit(ts)} FROM campaign_transactions WHERE id IN ({chunk}) AND record_state = 'OCR_UNREVIEWED' AND provenance = 'ocr'")
                    st.append(f"UPDATE campaign_transactions SET record_state = 'ACTIVE', updated_at = {lit(ts)} WHERE id IN ({chunk}) AND record_state = 'OCR_UNREVIEWED' AND provenance = 'ocr'")
            fp_rows = [t for t in (eff[fid] if apply else live_rows(rows_by[fid]))]
            checks = json.dumps({k: v for k, v in r["checks"].items()}, separators=(",", ":"))[:20000]
            st.append(f"INSERT OR REPLACE INTO clerk_reviews (filing_id, jurisdiction_id, verdict, reason, checks, rows_fp, applied, run_id, reviewed_at) VALUES ({lit(fid)}, {lit(r['jurisdiction_id'])}, {lit(r['verdict'])}, {lit(';'.join(r['fail']) or None)}, {lit(checks)}, {lit(rows_fp(fp_rows))}, {1 if apply else 0}, {lit(run_id)}, {lit(ts)})")
        report["finished_at"] = now()
        st.append(f"UPDATE clerk_review_runs SET finished_at = {lit(report['finished_at'])}, summary = {lit(json.dumps(place, separators=(',', ':')))} WHERE run_id = {lit(run_id)}")
        report["d1_requests"] = db.exec_many(st)
        report["flipped_confirmed"] = db.q(f"SELECT COUNT(*) AS n FROM clerk_review_flips WHERE run_id = {lit(run_id)}")[0]["n"]
    write_report(report, args.report)
    return 0


def revert(db, run_id):
    import d1 as d1mod
    lit, ts = d1mod.lit, now()
    st = [
        f"UPDATE campaign_transactions SET record_state = 'OCR_UNREVIEWED', updated_at = {lit(ts)} WHERE id IN (SELECT transaction_id FROM clerk_review_flips WHERE run_id = {lit(run_id)} AND reverted_at IS NULL) AND record_state = 'ACTIVE' AND provenance = 'ocr'",
        f"UPDATE campaign_transactions SET amount_minor = (SELECT CAST(before_value AS INTEGER) FROM clerk_corrections c WHERE c.run_id = {lit(run_id)} AND c.transaction_id = campaign_transactions.id AND c.field = 'amount_minor') WHERE id IN (SELECT transaction_id FROM clerk_corrections WHERE run_id = {lit(run_id)} AND field = 'amount_minor' AND reverted_at IS NULL)",
        f"UPDATE campaign_transactions SET record_state = 'OCR_UNREVIEWED', updated_at = {lit(ts)} WHERE id IN (SELECT transaction_id FROM clerk_corrections WHERE run_id = {lit(run_id)} AND field = 'record_state' AND reverted_at IS NULL) AND record_state IN ('DUPLICATE', 'OCR_REJECTED')",
        *[f"UPDATE campaign_transactions SET {fld} = (SELECT before_value FROM clerk_corrections c WHERE c.run_id = {lit(run_id)} AND c.transaction_id = campaign_transactions.id AND c.field = '{fld}'), updated_at = {lit(ts)} WHERE id IN (SELECT transaction_id FROM clerk_corrections WHERE run_id = {lit(run_id)} AND field = '{fld}' AND reverted_at IS NULL)"
          for fld in ("counterparty_name_raw", "transaction_date")],
        f"DELETE FROM campaign_transactions WHERE id IN (SELECT transaction_id FROM clerk_corrections WHERE run_id = {lit(run_id)} AND field = 'inserted' AND reverted_at IS NULL) AND source_key LIKE 'clerk:%' AND provenance = 'ocr'",
        f"UPDATE clerk_review_flips SET reverted_at = {lit(ts)} WHERE run_id = {lit(run_id)}",
        f"UPDATE clerk_corrections SET reverted_at = {lit(ts)} WHERE run_id = {lit(run_id)}",
        f"UPDATE clerk_reviews SET applied = 0, verdict = 'FLAG', reason = 'reverted:' || {lit(run_id)} WHERE run_id = {lit(run_id)}",
    ]
    print("bookmark before revert:", db.bookmark())
    db.exec_many(st)
    print(json.dumps({"reverted": run_id}))
    return 0


def write_report(report, path):
    js = json.dumps(report, indent=1, default=str)
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            f.write(js)
    md = [f"## Clerk review {report['run_id']}{' (dry run)' if report['dry_run'] else ''}", "",
          "| place | reviewed | PASS | CORRECTED | FLAG | deferred | rows -> ACTIVE | Raised $ | Spent $ | held |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for p, v in report["places"].items():
        md.append(f"| {p} | {v['reviewed']} | {v['PASS']} | {v['CORRECTED_PASS']} | {v['FLAG']} | {v['deferred']} | {v['flip_rows'] if not v['held'] else 0} | "
                  f"{v['raised_minor'] / 100:,.2f} | {v['spent_minor'] / 100:,.2f} | {'HELD: ' + '; '.join(v['hold_reasons'][:3]) if v['held'] else 'no'} |")
    if report.get("bookmark_before"):
        md += ["", f"D1 bookmark before writes: `{report['bookmark_before']}`"]
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    sys.exit(main())
