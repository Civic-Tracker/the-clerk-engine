# The Clerk Engine

Free-tier scripts that do Civic Tracker's repeat grind (fetch, fingerprint, diff, store, register) so Hark only does judgment work (OCR review, dedup calls, Deep Records, release decisions).

**Hard rules**
- STAGING ONLY: staging R2 `civic-tracker-bakeoff-evidence`, staging D1 `5caedb62…`. The adapter refuses any other target.
- Never publishes, never changes a jurisdiction's `site_status`, never touches prod Worker/D1/R2.
- Free tiers only (GitHub Actions minutes, Cloudflare free R2/D1). No new spend.
- Work lands on `hark/...` branches via PR; only Bryan merges `main`.
- Secrets live only in GitHub repo secrets / env. Never printed or committed.

## Layout
| Path | What |
|---|---|
| `adapters/netfile.py` | NetFile adapter (Step 1). Stdlib Python. |
| `lib/r2s3.py` | Minimal R2 S3 client (SigV4); short-lived, prefix-scoped temp creds from the Cloudflare token. |
| `lib/net.py` | Shared retry: exponential backoff + jitter on 429/5xx/timeouts (NetFile, D1, R2, Cloudflare API). |
| `tests/` | `python3 -m unittest discover -s tests` (stdlib, no network). |
| `packs/la-county-netfile.json` | Jurisdiction pack. **New city = one config row** (`slug`, `agency` code, `jurisdiction_id`). |
| `.github/workflows/netfile-nightly.yml` | Nightly 09:17 UTC + manual dispatch; per-city table on the run page (job summary); uploads the JSON report as an artifact. |
| `reports/` | Dry-run summary. Nightly JSON reports go to Actions artifacts (gitignored). |
| `docs/STEP2-mail-intake.md` | What Step 2 (Apps Script mail intake) needs. |
| `review/clerk_review.py` | Clerk review of OCR'd money (see below). Stdlib Python + tesseract/poppler. |
| `lib/form460.py`, `lib/pages.py`, `lib/d1.py` | Form 460 box reading, page text reads (A/B/C), staging-only D1 client. |
| `.github/workflows/clerk-review.yml` | Nightly 06:23 UTC (before the 12:15 AM PT Official App refresh) + manual dispatch. |

## NetFile adapter: what one run does
1. Lists every campaign filing for each pack row: `netfile.com/api/public/sites/api/filings/byDate` (current + archived).
2. Diffs against staging D1 `ingest_documents` where `source_key = 'netfile:<AGENCY>'` → only **new** filing ids are fetched.
   Filings NetFile marks `canDownload:false` (no image; server answers 500 ImageNotFound) are counted as `no_image_on_netfile` and skipped.
3. Downloads the original PDF (`netfile.com/Connect2/api/public/image/<filingId>`), sha256s it.
4. Stores write-once to R2 `raw/<slug>/<sha256>.pdf` (`If-None-Match: *`; HEAD first, so a file already there is deduped, never overwritten).
5. Registers a hidden row in staging D1 `ingest_documents`: id `nf_<agency>_<filingId>`, `status='pending'`, `text_status='not-run'`,
   title `[FPPC 460] [OCR_UNREVIEWED] <filer> - filed <YYYY-MM-DD Pacific>`.
6. Writes a per-city JSON report (counts + failures) and, in Actions, a per-city ok/FAILED table on the run page. Time-boxed (`--max-seconds`) and resumable: the next run picks up where it stopped.

**Failure rules.** Every network call retries 429/5xx/timeouts with exponential backoff + jitter. One city failing (e.g. NetFile down for that agency) is logged with a one-line reason and the run moves on to the next city. Exit code: `0` if at least one city ran OK; `1` if every city failed; `2` for a config or auth problem (token missing, wrong, or lacking D1/R2 scope — checked once up front, so a bad secret fails in seconds without partial writes).

Local run (same script the Action runs):
```
CLOUDFLARE_API_TOKEN=… python3 adapters/netfile.py --pack packs/la-county-netfile.json [--only calabasas] [--list-only]
```

## TODO for Bryan (Action secret)
The nightly Action needs one repo secret, which could not be set without exposing its value from the agent sandbox:
- Settings → Secrets and variables → Actions → New repository secret → **`CLOUDFLARE_API_TOKEN`**.
  Best: a **new** Cloudflare API token scoped to *Account › D1 › Edit* (staging DB) + *Account › Workers R2 Storage › Edit* only,
  rather than the full civic-tracker agent token. Free.
Until it is set, the workflow fails fast at "Guard - token present" (no partial writes). This is what failed the first scheduled run on Oct 16, 2026. Then run it once by hand from the Actions tab.

Free-tier note: private-repo Actions get 2,000 free minutes/month; a no-change nightly run over 5 cities takes ~1 minute.

## Clerk review: what one run does
Scanned Form 460s are OCR'd into staging D1 `campaign_transactions` as `record_state='OCR_UNREVIEWED'`, `provenance='ocr'`
(never counted). `review/clerk_review.py` checks each filing the way a clerk would before it counts:

1. **Two independent reads** of each Schedule A / C / E summary box (read A = the ingest's text; read B = fresh tesseract
   psm 4; read C at 400 dpi breaks a tie, 2 of 3 must agree on line 1).
2. **Reconcile**: OCR rows per schedule = that schedule's itemized line 1 (+/- $0.50). No box and no rows = 0.
3. **Not a duplicate / not superseded**: same rows as another OCR filing or as ACTIVE e-filed rows, an earlier
   amendment of the same filer + period, or overlapping periods of one committee are flagged.
4. **Clerk fixes** (logged before/after in `clerk_corrections`): a decimal slip, confusable digit, transposition, a row
   whose amount was not read, or a doubled row, fixed only when exactly one fix exists, the fixed value is printed on
   that page in an OCR read, and the rows then reconcile to the cent.

PASS / CORRECTED_PASS rows go `OCR_UNREVIEWED -> ACTIVE` (provenance stays `ocr`); every flip is in
`clerk_review_flips` (per run). Verdicts are in `clerk_reviews`. A Time Travel bookmark is taken before any write.
**Held batch**: a place gets no writes if a counted Raised/Spent total for a (place, year) that already has counted money
would move > 10% or > $250k, a filing is > 10x its committee's history, a passing filing overlaps ACTIVE e-filed rows,
or a non-460 row is in the batch. Years with no counted money yet are new coverage, not a move (reported per year).
Unresolved filings stay `OCR_UNREVIEWED`; the run report (ids and counts only) lists them with the gap and reason.

Nightly runs only look at new or changed filings (row fingerprint) and re-apply standing verdicts if a re-promote
reset the rows. Revert a run: `python3 review/clerk_review.py --revert <run_id>`.
```
CLOUDFLARE_API_TOKEN=... python3 review/clerk_review.py --places beverlyhills,culvercity [--dry-run] [--max-seconds N]
```
