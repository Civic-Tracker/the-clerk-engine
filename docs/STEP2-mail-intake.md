# Step 2: Apps Script mail intake on abc.tracker.records@gmail.com (plan, not built)

Goal: move the routine half of `pra-intake/mail-intake.mjs` (scan records inbox → store originals → register hidden) into a free Google Apps Script on a time trigger, so Hark only handles replies, judgment, and OCR review.

## What the script does (same rules as mail-intake.mjs)
- Every 15 min: Gmail search on the records inbox for new mail with attachments or public download links (NextRequest "Document Released", GovQA, Drive), skipping our own outbound request copies.
- For each attachment: sha256 (`Utilities.computeDigest`), PUT write-once to staging R2 `records-requests/<slug>/<received>/<sha8>-<clean-name>` (SigV4 signed in Apps Script, `If-None-Match: *`), then INSERT OR IGNORE into staging D1 `records_documents` (`review_status='PRIVATE_UNREVIEWED'`) and, for campaign forms, `ingest_documents` titled `[Form NNN] [OCR_UNREVIEWED] …`.
- Applies a Gmail label `clerk/stored` (no send, no reply, no delete; trash stays a Hark step under Bryan's Oct 13 rule).
- Appends one line per file to a "Clerk log" tab; Hark reads it daily and updates the Requests tab.
- Privacy carried over: requester identity tokens and request numbers scrubbed from filenames, never in R2 keys/metadata/D1.
- No OCR in Apps Script (6-min limit). OCR stays queued (`OCR_UNREVIEWED`) for an Actions Tesseract job (Step 3).

## What Bryan needs to do (about 10 minutes, free)
1. Sign in to script.google.com as abc.tracker.records@gmail.com and turn on **Google Apps Script API** at script.google.com/home/usersettings (lets Hark push the code with the connected Google account).
2. In Cloudflare, create two narrowly scoped free credentials and paste them into the script's **Script Properties** (Hark never sees them):
   - an **R2 API token** (Access Key ID + Secret) limited to bucket `civic-tracker-bakeoff-evidence`, Object Read & Write;
   - an **API token** with *D1 › Edit* only (used for the staging D1 query endpoint).
3. Approve the script's one-time OAuth prompt (Gmail read + label, Sheets append, external fetch).
4. Say which slug map to use for agency senders (Hark will seed it from the current Requests sheet).

## What Hark does once those exist
- Port `classify`, `sanitizeFilename`, link extraction, and the `buildStmts` SQL from `pra-intake/picorivera-drive/mi-lib.mjs` into Apps Script (`clerk-mail.gs`), commit it to this repo under `apps-script/`, push via the Apps Script API, and run a dry run (no writes) against the last 14 days of mail before enabling the trigger.

## Free-tier limits that matter (consumer Gmail account)
6 min per execution, 90 min/day of trigger runtime, 20,000 UrlFetch calls/day, 50 MB max per fetch payload. Large productions (>50 MB) are left for Hark.
