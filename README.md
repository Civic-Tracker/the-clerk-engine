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
| `packs/la-county-netfile.json` | Jurisdiction pack. **New city = one config row** (`slug`, `agency` code, `jurisdiction_id`). |
| `.github/workflows/netfile-nightly.yml` | Nightly 09:30 UTC + manual dispatch; uploads the run report as an artifact. |
| `reports/` | Dry-run summary. Nightly JSON reports go to Actions artifacts (gitignored). |
| `docs/STEP2-mail-intake.md` | What Step 2 (Apps Script mail intake) needs. |

## NetFile adapter: what one run does
1. Lists every campaign filing for each pack row: `netfile.com/api/public/sites/api/filings/byDate` (current + archived).
2. Diffs against staging D1 `ingest_documents` where `source_key = 'netfile:<AGENCY>'` → only **new** filing ids are fetched.
   Filings NetFile marks `canDownload:false` (no image; server answers 500 ImageNotFound) are counted as `no_image_on_netfile` and skipped.
3. Downloads the original PDF (`netfile.com/Connect2/api/public/image/<filingId>`), sha256s it.
4. Stores write-once to R2 `raw/<slug>/<sha256>.pdf` (`If-None-Match: *`; HEAD first, so a file already there is deduped, never overwritten).
5. Registers a hidden row in staging D1 `ingest_documents`: id `nf_<agency>_<filingId>`, `status='pending'`, `text_status='not-run'`,
   title `[FPPC 460] [OCR_UNREVIEWED] <filer> - filed <YYYY-MM-DD Pacific>`.
6. Writes a per-city JSON report (counts + failures). Time-boxed (`--max-seconds`) and resumable: the next run picks up where it stopped.

Local run (same script the Action runs):
```
CLOUDFLARE_API_TOKEN=… python3 adapters/netfile.py --pack packs/la-county-netfile.json [--only calabasas] [--list-only]
```

## TODO for Bryan (Action secret)
The nightly Action needs one repo secret, which could not be set without exposing its value from the agent sandbox:
- Settings → Secrets and variables → Actions → New repository secret → **`CLOUDFLARE_API_TOKEN`**.
  Best: a **new** Cloudflare API token scoped to *Account › D1 › Edit* (staging DB) + *Account › Workers R2 Storage › Edit* only,
  rather than the full civic-tracker agent token. Free.
Until it is set, the workflow fails fast at "Guard - token present" (no partial writes). Then run it once by hand from the Actions tab.

Free-tier note: private-repo Actions get 2,000 free minutes/month; a no-change nightly run over 5 cities takes ~1 minute.
