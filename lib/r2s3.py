#!/usr/bin/env python3
"""Minimal R2 S3 client (SigV4, stdlib). Creds derived from CLOUDFLARE_API_TOKEN:
access key = token id (from /user/tokens/verify), secret = sha256(token).
Usage: r2s3.py put <bucket> <key> <file> <meta-json>   (If-None-Match:* -> never overwrites)
       r2s3.py head <bucket> <key>
       r2s3.py get <bucket> <key> <outfile>
"""
import sys, os, json, hashlib, hmac, base64, datetime, urllib.request, urllib.parse, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import net

ACCT = "1237f978938188ed96dc859fdd2d40d0"
HOST = f"{ACCT}.r2.cloudflarestorage.com"
CF = "https://api.cloudflare.com/client/v4"


def tok():
    """Read the token when needed (not at import), so a missing secret is a clear error, not a KeyError on import."""
    t = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not t:
        raise net.AuthError("CLOUDFLARE_API_TOKEN is not set")
    return t


def cf(method, path, body=None):
    """Cloudflare API call with retries (429/5xx/timeouts). 401/403 -> AuthError."""
    data = json.dumps(body).encode() if body is not None else None
    hd = {"Authorization": f"Bearer {tok()}"}
    if data is not None:
        hd["content-type"] = "application/json"
    s, _, b = net.request(urllib.request.Request(CF + path, data=data, method=method, headers=hd), timeout=60)
    try:
        j = json.loads(b or b"{}")
    except ValueError:
        j = {}
    if s in (401, 403):
        raise net.AuthError(f"Cloudflare {path.split('?')[0]} HTTP {s}: {json.dumps(j.get('errors'))[:160]}")
    if s != 200 or not j.get("success"):
        raise RuntimeError(f"Cloudflare {path.split('?')[0]} HTTP {s}: {json.dumps(j.get('errors'))[:160]}")
    return j["result"]


_TOKEN_ID = None
def token_id():
    """Id of the API token (= R2 parent access key). User tokens verify at /user/tokens/verify,
    account-owned tokens at /accounts/<id>/tokens/verify; try both."""
    global _TOKEN_ID
    if _TOKEN_ID:
        return _TOKEN_ID
    errs = []
    for path in ("/user/tokens/verify", f"/accounts/{ACCT}/tokens/verify"):
        try:
            _TOKEN_ID = cf("GET", path)["id"]
            return _TOKEN_ID
        except (net.AuthError, RuntimeError) as e:
            errs.append(str(e)[:120])
    raise net.AuthError("token verify failed: " + " | ".join(errs))


def temp_creds(bucket, prefix, perm="object-read-write"):
    try:
        j = cf("POST", f"/accounts/{ACCT}/r2/temp-access-credentials",
               {"bucket": bucket, "parentAccessKeyId": token_id(), "permission": perm, "ttlSeconds": 900, "prefixes": [prefix]})
    except net.AuthError as e:
        raise net.AuthError(f"R2 temp credentials refused (token needs Account > Workers R2 Storage > Edit): {e}")
    return j["accessKeyId"], j["secretAccessKey"], j["sessionToken"]

AK = SK = ST = None
def use(bucket, prefix, perm="object-read-write"):
    global AK, SK, ST
    AK, SK, ST = temp_creds(bucket, prefix, perm)

def sign(method, bucket, key, headers, payload_hash):
    now = datetime.datetime.now(datetime.timezone.utc)
    amz = now.strftime("%Y%m%dT%H%M%SZ"); ds = now.strftime("%Y%m%d")
    uri = "/" + bucket + "/" + "/".join(urllib.parse.quote(s, safe="-_.~") for s in key.split("/"))
    h = {k.lower(): v.strip() for k, v in headers.items()}
    h["host"] = HOST; h["x-amz-security-token"] = ST; h["x-amz-date"] = amz; h["x-amz-content-sha256"] = payload_hash
    sh = sorted(h)
    creq = "\n".join([method, uri, "", "".join(f"{k}:{h[k]}\n" for k in sh), ";".join(sh), payload_hash])
    scope = f"{ds}/auto/s3/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(creq.encode()).hexdigest()])
    k = ("AWS4" + SK).encode()
    for p in (ds, "auto", "s3", "aws4_request"):
        k = hmac.new(k, p.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    h["authorization"] = f"AWS4-HMAC-SHA256 Credential={AK}/{scope}, SignedHeaders={';'.join(sh)}, Signature={sig}"
    return "https://" + HOST + uri, h

def req(method, bucket, key, headers=None, body=b""):
    ph = hashlib.sha256(body).hexdigest()
    # Retries 429/5xx/timeouts. Each attempt re-signs (fresh x-amz-date). 412 (exists) / 404 / 403 come straight back.
    last = None
    for attempt in range(5):
        url, h = sign(method, bucket, key, headers or {}, ph)
        r = urllib.request.Request(url, data=body if method == "PUT" else None, method=method, headers=h)
        try:
            s, hd, out = net.request(r, timeout=120, tries=1)
        except RuntimeError as e:
            last = str(e)
        else:
            if s not in net.RETRY_STATUS:
                return s, dict(hd or {}), out
            last = f"HTTP {s}"
        if attempt < 4:
            net.time.sleep(net.backoff(attempt))
    raise RuntimeError(f"R2 {method} {key[-40:]} failed after 5 tries: {last}")

if __name__ == "__main__":
    cmd = sys.argv[1]
    use(sys.argv[2], sys.argv[3].rsplit("/", 1)[0] + "/")
    if cmd == "put":
        b, k, f, meta = sys.argv[2], sys.argv[3], sys.argv[4], json.loads(sys.argv[5])
        body = open(f, "rb").read()
        hd = {"content-type": os.environ.get("CT", "application/pdf"), "if-none-match": "*",
              "x-amz-checksum-sha256": base64.b64encode(hashlib.sha256(body).digest()).decode()}
        for mk, mv in meta.items():
            hd["x-amz-meta-" + mk.lower()] = urllib.parse.quote(str(mv), safe=" <>@.,:;()'-_/+=!*&#")
        # Derived text copies may be replaced (re-OCR); originals stay write-once. Opt-in + path-restricted.
        if os.environ.get("R2_REPLACE_TEXT") == "1" and "/_index/text/" in k and k.endswith(".txt"):
            hd.pop("if-none-match")
        s, h, out = req("PUT", b, k, hd, body)
        print(json.dumps({"status": s, "etag": h.get("ETag") or h.get("etag"), "body": out.decode()[:500]}))
    elif cmd == "head":
        s, h, _ = req("HEAD", sys.argv[2], sys.argv[3])
        print(json.dumps({"status": s, "headers": h}))
    elif cmd == "get":
        s, h, out = req("GET", sys.argv[2], sys.argv[3])
        open(sys.argv[4], "wb").write(out)
        print(json.dumps({"status": s, "len": len(out)}))
