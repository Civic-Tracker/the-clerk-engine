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
MOVE_PCT, MOVE_ABS = 0.10, 25_000_000   # 10% / $250,000 in cents
OOM_MIN = 5_000_000           # $50,000
SCHED = {"460A": "A", "460C": "C", "460E": "E"}
DUP_REASONS = {"duplicate_copy", "superseded_by_amendment", "amendment_order_unknown", "overlapping_periods",
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


def agreed_box(pages, slug, sha, key, page, sched, deadline):
    """Read the schedule box on `page` with reads A and B (C on disagreement). Returns (l1 or None, detail)."""
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
    vals = [v for v in (la, lb, lc) if v is not None]
    top = Counter(vals).most_common(1)
    if top and top[0][1] >= 2:
        return top[0][0], det
    return None, det


def review_filing(f, rows, pages, deadline):
    """Checks 1 and 2 (+ clerk fixes) for one filing. Returns a result dict."""
    slug, sha, key = slug_of(f["jurisdiction_id"]), f["source_hash"], f["evidence_r2_key"]
    res = {"filing_id": f["id"], "jurisdiction_id": f["jurisdiction_id"], "checks": {}, "corrections": [],
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
        if junk:
            res["fail"].append(f"{s}:row_name_is_form_text")
        chk = {"rows": len(srows), "null_rows": sum(r["amount_minor"] is None for r in srows), "form_text_names": len(junk),
               "rows_sum": sum(r["amount_minor"] or 0 for r in srows)}
        if not box_pages:
            chk["itemized"] = 0 if not srows else None
            chk["box"] = "none"
            if srows:
                res["fail"].append(f"{s}:no_summary_box")
                res["gap"][s] = None
            res["checks"][s] = chk
            continue
        reads = []
        for p in box_pages[:3]:
            l1, det = agreed_box(pages, slug, sha, key, p, s, deadline)
            reads.append((l1, det))
        chk["reads"] = [d for _, d in reads]
        vals = {l1 for l1, _ in reads if l1 is not None}
        if len(vals) != 1:
            chk["itemized"] = None
            res["fail"].append(f"{s}:summary_reads_disagree" if not vals else f"{s}:multiple_boxes_differ")
            res["gap"][s] = None
            res["checks"][s] = chk
            continue
        l1 = vals.pop()
        chk["itemized"] = l1
        if s == "A" and summ.get(1) is not None:
            chk["summary_page_line1"] = summ.get(1)
        if s == "E" and summ.get(6) is not None:
            chk["summary_page_line6"] = summ.get(6)
        gap = chk["rows_sum"] - l1
        if abs(gap) <= TOL:
            chk["ok"] = True
            res["checks"][s] = chk
            continue
        fix = clerk_fix(srows, gap, pages, slug, sha, key, deadline)
        if fix:
            chk["ok"] = True
            chk["fixed"] = fix["rule"]
            if fix["rule"] == "drop_duplicate_row":
                res["drop"].append(fix)
            else:
                res["corrections"].append(fix)
        else:
            chk["ok"] = False
            res["fail"].append(f"{s}:rows_do_not_reconcile")
            res["gap"][s] = gap
        res["checks"][s] = chk
    return res


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


def prefetch(todo, filings, pages, deadline, workers=None):
    """Run read B for every summary-box page of the filings to review, in parallel, until the deadline."""
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
                    jobs.append((slug, sha, key, p))

    jobs = sorted(set(jobs))

    def one(j):
        if time.time() > deadline:
            return
        pages.read("B", *j, box=True)
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


def dup_groups(filings, rows_by, efile_keys):
    """{filing_id: (reason, other)} for OCR filings that are duplicates or superseded."""
    out = {}
    sig = {}
    for fid, f in filings.items():
        sig[fid] = Counter((r["schedule_code"], r["amount_minor"], r["transaction_date"]) for r in live_rows(rows_by.get(fid, []))
                           if r["amount_minor"] is not None and r["transaction_date"])
    by_j = defaultdict(list)
    for fid, f in filings.items():
        if live_rows(rows_by.get(fid, [])):   # filings whose rows the ingest already superseded are out
            by_j[f["jurisdiction_id"]].append(fid)
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
                pa, pb = (fa["period_start"], fa["period_end"]), (fb["period_start"], fb["period_end"])
                same_period = bool(pa[1]) and pa == pb
                overlap_period = all(pa) and all(pb) and pa[0] <= pb[1] and pb[0] <= pa[1]
                same_filer = bool(fa["filer_identity_id"] and fa["filer_identity_id"] == fb["filer_identity_id"]) or \
                    bool(name_key(fa["filer_name_raw"]) and name_key(fa["filer_name_raw"]) == name_key(fb["filer_name_raw"]))
                sa, sb = sig[a], sig[b]
                small = min(sum(sa.values()), sum(sb.values()))
                ov = sum((sa & sb).values()) / small if small else 0.0
                identical = small >= 3 and sa == sb
                if same_filer and (overlap_period or (small >= 5 and ov >= 0.5)):
                    union(a, b)
                elif identical:
                    union(a, b)
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
            periods = {(filings[m]["period_start"], filings[m]["period_end"]) for m in ms}
            if len(periods) > 1 and not all(sig[m] == sig[ms[0]] for m in ms):
                for m in ms:                  # overlapping periods of one committee: counting both double counts
                    out[m] = ("overlapping_periods", ",".join(x for x in ms if x != m))
                continue

            def rank(fid):
                f = filings[fid]
                return (f["amendment_number"] or 0, f["filing_date"] or "")
            best = max(ms, key=rank)
            ties = [m for m in ms if rank(m) == rank(best)]
            if len(ties) > 1:
                if all(sig[m] == sig[ties[0]] for m in ties):
                    keep = min(ties)          # re-released copy of the same statement: keep one, deterministic
                    for m in ms:
                        if m != keep:
                            out[m] = ("duplicate_copy", keep)
                else:
                    for m in ms:              # original + amendment, can't tell which is newer: a clerk decides
                        out[m] = ("amendment_order_unknown", ",".join(x for x in ms if x != m))
                continue
            for m in ms:
                if m != best:
                    out[m] = ("superseded_by_amendment", best)
        for a, bs in cross.items():
            if a not in out:
                out[a] = ("possible_duplicate_other_filer", ",".join(sorted(bs)))
    # same rows as ACTIVE e-filed rows in the same place
    for fid, f in filings.items():
        if fid in out:
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


# ----------------------------------------------------------------------------- main
def load(d1, places):
    jids = ",".join(F_lit(f"jurisdiction-{p}") for p in places)
    tx = d1.all(f"""SELECT id, jurisdiction_id, filing_id, schedule_code, transaction_kind, amount_minor, transaction_date,
        record_state, provenance, counterparty_name_raw, cycle_year, raw_json FROM campaign_transactions
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
    dups = dup_groups(filings, rows_by, efile_keys)

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
            if was_dup == (fid in dups):
                continue                     # same rows, same duplicate status: the flag stands
            todo.append(fid)
        else:
            todo.append(fid)

    log(f'loaded {len(filings)} filings; todo {len(todo)} reapply {len(reapply)}')
    prefetch([f for f in todo if f not in dups], filings, pages, deadline)
    log(f'prefetch done ocr={pages.stats}')
    results, deferred = {}, []
    for fid in todo:
        f = filings[fid]
        if fid in dups:                       # a duplicate / superseded / overlapping filing never counts: no reads needed
            results[fid] = {"filing_id": fid, "jurisdiction_id": f["jurisdiction_id"], "verdict": "FLAG", "corrections": [], "drop": [],
                            "fail": [dups[fid][0]], "gap": {}, "checks": {"duplicate": {"reason": dups[fid][0], "other": dups[fid][1]}}}
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
    for fid in reapply:
        p = prior[fid]
        results[fid] = {"filing_id": fid, "jurisdiction_id": filings[fid]["jurisdiction_id"], "verdict": p["verdict"],
                        "checks": json.loads(p["checks"] or "{}"), "corrections": [], "drop": [], "fail": [], "gap": {}, "reapply": True}
        if fid in dups:                       # a newer duplicate/amendment showed up since: re-flag
            results[fid].update(verdict="FLAG", fail=[dups[fid][0]], reapply=False)

    # ---- per place money + major-discrepancy checks
    base = defaultdict(int)                 # (jid, year, sched) -> counted cents now
    for r in ef:
        base[(r["jurisdiction_id"], r["cycle_year"], r["schedule_code"])] += r["amount_minor"] or 0
    corr_amt = {c["transaction_id"]: c["after"] for r in results.values() for c in r["corrections"]}
    dropped = {c["transaction_id"] for r in results.values() for c in r["drop"]}
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
        for t in live_rows(rows_by[fid]):
            if t["record_state"] != "OCR_UNREVIEWED" or t["id"] in dropped:
                continue
            amt = corr_amt.get(t["id"], t["amount_minor"])
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
        a_tot = sum(corr_amt.get(t["id"], t["amount_minor"]) or 0 for t in live_rows(rows_by[fid]) if t["schedule_code"] == "460A")
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
                      "gap_minor": gaps, "auto_excluded": reason.startswith(("duplicate_copy", "superseded", "duplicate_of_efile"))})
    corrections = [{"filing_id": fid, **c} for fid, r in results.items() if r["verdict"] == "CORRECTED_PASS"
                   for c in r["corrections"] + r["drop"]]

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
                    st.append(f"UPDATE campaign_transactions SET amount_minor = {lit(c['after'])}, updated_at = {lit(ts)} WHERE id = {lit(c['transaction_id'])} AND amount_minor IS {lit(c['before'])} AND provenance = 'ocr'")
                for c in r["drop"]:
                    st.append(f"UPDATE campaign_transactions SET record_state = 'DUPLICATE', updated_at = {lit(ts)} WHERE id = {lit(c['transaction_id'])} AND record_state = 'OCR_UNREVIEWED' AND provenance = 'ocr'")
                for c in r["corrections"] + r["drop"]:
                    st.append(f"INSERT OR REPLACE INTO clerk_corrections (run_id, transaction_id, filing_id, field, before_value, after_value, rule, evidence, corrected_at) VALUES ({lit(run_id)}, {lit(c['transaction_id'])}, {lit(fid)}, {lit(c['field'])}, {lit(c['before'])}, {lit(c['after'])}, {lit(c['rule'])}, {lit('page ' + str(c.get('page')))}, {lit(ts)})")
                ids = flips.get(fid, [])
                for i in range(0, len(ids), 200):
                    chunk = ",".join(lit(x) for x in ids[i:i + 200])
                    st.append(f"INSERT OR IGNORE INTO clerk_review_flips (run_id, transaction_id, filing_id, from_state, to_state, flipped_at) SELECT {lit(run_id)}, id, filing_id, record_state, 'ACTIVE', {lit(ts)} FROM campaign_transactions WHERE id IN ({chunk}) AND record_state = 'OCR_UNREVIEWED' AND provenance = 'ocr'")
                    st.append(f"UPDATE campaign_transactions SET record_state = 'ACTIVE', updated_at = {lit(ts)} WHERE id IN ({chunk}) AND record_state = 'OCR_UNREVIEWED' AND provenance = 'ocr'")
            lr = [t for t in live_rows(rows_by[fid]) if t["id"] not in dropped]
            fp_rows = [dict(t, amount_minor=corr_amt.get(t["id"], t["amount_minor"])) for t in lr] if apply else lr
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
        f"UPDATE campaign_transactions SET record_state = 'OCR_UNREVIEWED', updated_at = {lit(ts)} WHERE id IN (SELECT transaction_id FROM clerk_corrections WHERE run_id = {lit(run_id)} AND field = 'record_state' AND reverted_at IS NULL) AND record_state = 'DUPLICATE'",
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
