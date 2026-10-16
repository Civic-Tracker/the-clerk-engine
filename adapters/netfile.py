#!/usr/bin/env python3
"""The Clerk Engine - NetFile adapter (Step 1, DRY RUN -> STAGING only).

For each enabled netfile row in a jurisdiction pack:
  1. list every campaign filing from NetFile's public API (current + archived),
  2. diff against what staging D1 already registered (ingest_documents, source_key netfile:<AGENCY>),
  3. download only NEW filings, sha256 them, store originals write-once to staging R2 raw/<slug>/<sha256>.pdf,
  4. register each stored file in staging D1 ingest_documents (status pending, title tagged [OCR_UNREVIEWED]).
Never touches prod, never publishes, never changes a jurisdiction's site_status.

Stdlib only. Auth: CLOUDFLARE_API_TOKEN env (D1 query API + R2 temp creds). Never printed.
Usage: python3 adapters/netfile.py --pack packs/la-county-netfile.json [--only slug,...] [--max-seconds N] [--limit N] [--list-only]
"""
import argparse, datetime, hashlib, json, os, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import net
from net import AuthError

UA = "CivicTracker-Clerk/0.1 (+https://civictracker.app; public campaign filings)"
LIST = "https://netfile.com/api/public/sites/api/filings/byDate?agencyCode={a}&startDate=1990-01-01&endDate=2100-01-01&page={p}&pageSize={n}&isArchived={ar}&isOnlyCampaign=true"
IMAGE = "https://netfile.com/Connect2/api/public/image/{id}"
PROD_D1_GUARD = {"5caedb62-49c2-437b-a87a-b85923911ad3"}   # staging D1 is the only allowed target
STAGING_BUCKETS = {"civic-tracker-bakeoff-evidence"}


def http(url, accept="application/json", timeout=90, tries=5):
    """NetFile GET with exponential backoff + jitter on 429/5xx/timeouts (lib/net.py).
    PDF 500 = ImageNotFoundException (no image on file): returned at once, never retried."""
    pdf = accept == "application/pdf"
    s, h, b = net.request(urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept}), timeout=timeout,
                          tries=tries, no_retry=lambda code: pdf and code == 500)
    if s != 200:
        return s, "", b""
    return s, (h.get("content-type", "") if h else ""), b


def list_filings(agency):
    out = {}
    for ar in ("false", "true"):
        p = 1
        while True:
            s, _, b = http(LIST.format(a=agency, p=p, n=200, ar=ar))
            if s != 200:
                raise RuntimeError(f"list {agency} archived={ar} page {p}: HTTP {s}")
            j = json.loads(b)
            for f in j.get("filings", []):
                f["_archived"] = ar == "true"
                out[str(f["id"])] = f
            if p * 200 >= j.get("totalCount", 0) or not j.get("filings"):
                break
            p += 1
    return out


class D1:
    def __init__(self, acct, db):
        if db not in PROD_D1_GUARD:
            raise SystemExit(f"refusing: D1 {db} is not the staging database")
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/d1/database/{db}/query"
        self.tok = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not self.tok:
            raise AuthError("CLOUDFLARE_API_TOKEN is not set")

    def q(self, sql, params=None):
        """D1 query with retries on 429/5xx/timeouts. Statements here are SELECT or INSERT OR IGNORE, so a retry is idempotent."""
        body = json.dumps({"sql": sql, "params": params} if params else {"sql": sql}).encode()
        r = urllib.request.Request(self.url, data=body, method="POST",
                                   headers={"Authorization": f"Bearer {self.tok}", "content-type": "application/json"})
        s, _, b = net.request(r, timeout=90)
        try:
            j = json.loads(b or b"{}")
        except ValueError:
            j = {}
        if s in (401, 403):
            raise AuthError(f"D1 HTTP {s} (token needs Account > D1 > Edit): {json.dumps(j.get('errors'))[:160]}")
        if s != 200 or not j.get("success"):
            raise RuntimeError(f"D1 HTTP {s}: {json.dumps(j.get('errors'))[:200]}")
        return j["result"]


def sqlq(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def ymd(netfile_date):
    """NetFile filingDate is ISO UTC (e.g. 2026-07-31T00:00:00Z); return the Pacific calendar date."""
    try:
        from zoneinfo import ZoneInfo
        d = datetime.datetime.fromisoformat(netfile_date.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        return None


def run_city(row, tgt, d1, args, deadline, rep):
    import r2s3
    slug, agency = row["slug"], row["agency"]
    source_key = f"netfile:{agency}"
    filings = list_filings(agency)
    known = {r["source_document_id"]: r["sha256"] for r in
             d1.q("SELECT source_document_id, sha256 FROM ingest_documents WHERE source_key = ?", [source_key])[0]["results"]}
    no_image = [i for i in filings if i not in known and filings[i].get("canDownload") is False]  # NetFile has no PDF (500 ImageNotFound); skip, never retry-spam
    new_ids = sorted((i for i in filings if i not in known and i not in no_image), key=lambda i: filings[i].get("filingDate", ""))
    c = rep.setdefault(slug, {"agency": agency, "filings_found": len(filings), "already_registered": len(known),
                              "new_listed": len(new_ids), "downloaded": 0, "stored_new_r2": 0, "already_in_r2": 0,
                              "bytes_stored": 0, "d1_registered": 0, "not_downloadable": 0, "errors": [], "remaining": 0})
    c.update(filings_found=len(filings), already_registered=len(known), new_listed=len(new_ids), no_image_on_netfile=len(no_image))
    if args.list_only:
        c["remaining"] = len(new_ids)
        return
    prefix = f"{tgt['r2_prefix']}/{slug}/"
    r2s3.use(tgt["r2_bucket"], prefix); creds_at = time.time()
    pending_sql = []
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    done = 0
    for fid in new_ids:
        if time.time() > deadline or (args.limit and done >= args.limit):
            break
        f = filings[fid]
        if time.time() - creds_at > 600:
            r2s3.use(tgt["r2_bucket"], prefix); creds_at = time.time()
        try:
            s, ct, body = http(IMAGE.format(id=fid), accept="application/pdf", timeout=120)
            if s != 200 or not body.startswith(b"%PDF"):
                c["not_downloadable"] += 1
                c["errors"].append({"filing": fid, "why": f"image HTTP {s} {ct[:30]} canDownload={f.get('canDownload')}"})
                done += 1
                continue
            c["downloaded"] += 1
            sha = hashlib.sha256(body).hexdigest()
            key = f"{prefix}{sha}.pdf"
            hs, _, _ = r2s3.req("HEAD", tgt["r2_bucket"], key)
            if hs == 200:
                c["already_in_r2"] += 1
            else:
                import base64, urllib.parse
                meta = {"sha256": sha, "source": f"netfile:{agency}:{fid}", "form": f.get("formName", ""),
                        "filer": f.get("filerName", "")[:150], "filed": ymd(f.get("filingDate", "")) or "",
                        "retrieved": now, "review": "OCR_UNREVIEWED"}
                hd = {"content-type": "application/pdf", "if-none-match": "*",
                      "x-amz-checksum-sha256": base64.b64encode(hashlib.sha256(body).digest()).decode()}
                for mk, mv in meta.items():
                    hd["x-amz-meta-" + mk] = urllib.parse.quote(str(mv), safe=" <>@.,:;()'-_/+=!*&#")
                ps, _, pb = r2s3.req("PUT", tgt["r2_bucket"], key, hd, body)
                if ps == 412:
                    c["already_in_r2"] += 1
                elif ps != 200:
                    raise RuntimeError(f"R2 put {ps} {pb[:120]!r}")
                else:
                    c["stored_new_r2"] += 1; c["bytes_stored"] += len(body)
            filed = ymd(f.get("filingDate", ""))
            title = f"[{f.get('formName','?')}] [OCR_UNREVIEWED] {f.get('filerName','').strip()} - filed {filed}"
            pending_sql.append(
                "INSERT OR IGNORE INTO ingest_documents (id, jurisdiction_slug, source_key, source_document_id, title, doc_year, "
                "source_url, retrieved_at, sha256, bytes, r2_key, status, text_status, page_count, ocr_pages, text_layer_pages, jurisdiction_id) VALUES ("
                + ", ".join(sqlq(v) for v in [f"nf_{agency.lower()}_{fid}", slug, source_key, fid, title,
                                               int(filed[:4]) if filed else None, IMAGE.format(id=fid), now, sha, len(body),
                                               key, "pending", "not-run", None, None, None, row["jurisdiction_id"]]) + ");")
        except AuthError:
            raise
        except Exception as e:
            c["errors"].append({"filing": fid, "why": str(e)[:160]})
        done += 1
        if len(pending_sql) >= 25:
            d1.q("\n".join(pending_sql)); c["d1_registered"] += len(pending_sql); pending_sql = []
        time.sleep(args.pace)
    if pending_sql:
        d1.q("\n".join(pending_sql)); c["d1_registered"] += len(pending_sql)
    c["remaining"] = len(new_ids) - done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--max-seconds", type=int, default=3000)
    ap.add_argument("--limit", type=int, default=0, help="max new filings per city per run (0 = all)")
    ap.add_argument("--pace", type=float, default=0.4)
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--report", default="reports/latest.json")
    args = ap.parse_args()
    pack = json.load(open(args.pack))
    tgt = pack["target"]
    if tgt.get("env") != "staging" or tgt["r2_bucket"] not in STAGING_BUCKETS:
        raise SystemExit("refusing: pack target is not staging")
    only = set(filter(None, (x.strip() for x in args.only.split(","))))
    rows = [r for r in pack["jurisdictions"]
            if r.get("platform") == "netfile" and r.get("enabled") and (not only or r["slug"] in only)]
    unknown = only - {r["slug"] for r in rows}
    deadline = time.time() + args.max_seconds
    rep = json.load(open(args.report)) if os.path.exists(args.report) else {}
    rep.pop("_run", None)
    status = {}   # slug -> (ok|failed|skipped, one-line reason)
    fatal = None
    if unknown:
        fatal = f"config: --only names unknown/disabled slug(s): {', '.join(sorted(unknown))}"
    elif not rows:
        fatal = "config: no enabled netfile rows selected"
    else:
        try:  # preflight: token present + D1 reachable, so a bad secret fails once, loudly
            d1 = D1(tgt["account_id"], tgt["d1_database_id"])
            d1.q("SELECT 1")
        except AuthError as e:
            fatal = f"auth: {e}"
        except Exception as e:
            fatal = f"preflight: staging D1 unreachable: {str(e)[:160]}"
    for row in ([] if fatal else rows):
        slug = row["slug"]
        if time.time() > deadline:
            status[slug] = ("skipped", "time box reached before this city")
            continue
        try:
            run_city(row, tgt, d1, args, deadline, rep)
            c = rep.get(slug, {})
            n_err = len(c.get("errors", []))
            status[slug] = ("ok", f"{c.get('d1_registered', 0)} registered, {c.get('stored_new_r2', 0)} new PDFs"
                            + (f", {n_err} filing errors" if n_err else "") + (f", {c['remaining']} left for next run" if c.get("remaining") else ""))
        except AuthError as e:  # bad/under-scoped token: every city would fail the same way
            fatal = f"auth: {e}"
            status[slug] = ("failed", str(e)[:160])
            rep.setdefault(slug, {"errors": []}).setdefault("errors", []).append({"city": str(e)[:200]})
            break
        except Exception as e:  # one city fails alone; the rest still run
            status[slug] = ("failed", str(e)[:160].replace("\n", " "))
            rep.setdefault(slug, {"errors": []}).setdefault("errors", []).append({"city": str(e)[:200]})
        print(slug, status[slug][0], json.dumps({k: v for k, v in rep.get(slug, {}).items() if k != "errors"}),
              "errors", len(rep.get(slug, {}).get("errors", [])), flush=True)
    for row in rows:
        status.setdefault(row["slug"], ("skipped", "not run (stopped by auth error)" if fatal else "not run"))
    ok = [s for s, (st, _) in status.items() if st == "ok"]
    failed = [s for s, (st, _) in status.items() if st == "failed"]
    if fatal:
        exit_code, verdict = 2, f"FAILED - {fatal}"
    elif rows and not ok:
        exit_code, verdict = 1, "FAILED - every city failed"
    else:
        exit_code, verdict = 0, f"OK - {len(ok)} ok, {len(failed)} failed" + (" (see reasons)" if failed else "")
    rep["_run"] = {"finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "mode": "staging-dry-run",
                   "list_only": args.list_only, "verdict": verdict, "exit_code": exit_code,
                   "cities": {s: {"status": st, "reason": why} for s, (st, why) in status.items()}}
    os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
    json.dump(rep, open(args.report, "w"), indent=1)
    write_summary(rep, status, verdict, args)
    print(verdict, flush=True)
    if fatal:
        print(f"::error::{fatal}", flush=True)
    for s in failed:
        print(f"::warning::{s} failed: {status[s][1]}", flush=True)
    return exit_code


def write_summary(rep, status, verdict, args):
    """Markdown table for the Actions run page ($GITHUB_STEP_SUMMARY); no-op locally."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"## Clerk Engine - NetFile -> staging{' (list only)' if args.list_only else ''}", "", f"**{verdict}**", "",
             "| City | Status | Found | New | Registered | New PDFs | Note |", "|---|---|---|---|---|---|---|"]
    for slug, (st, why) in status.items():
        c = rep.get(slug, {})
        icon = {"ok": "ok", "failed": "FAILED", "skipped": "skipped"}[st]
        lines.append(f"| {slug} | {icon} | {c.get('filings_found', '')} | {c.get('new_listed', '')} | "
                     f"{c.get('d1_registered', '')} | {c.get('stored_new_r2', '')} | {why.replace('|', '/')} |")
    lines += ["", "Staging only (staging D1 + staging R2, hidden rows). Full JSON: artifact `clerk-netfile-report`."]
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
