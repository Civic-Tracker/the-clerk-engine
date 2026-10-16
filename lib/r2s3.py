#!/usr/bin/env python3
"""Minimal R2 S3 client (SigV4, stdlib). Creds derived from CLOUDFLARE_API_TOKEN:
access key = token id (from /user/tokens/verify), secret = sha256(token).
Usage: r2s3.py put <bucket> <key> <file> <meta-json>   (If-None-Match:* -> never overwrites)
       r2s3.py head <bucket> <key>
       r2s3.py get <bucket> <key> <outfile>
"""
import sys, os, json, hashlib, hmac, base64, datetime, urllib.request, urllib.parse, urllib.error

ACCT = "1237f978938188ed96dc859fdd2d40d0"
HOST = f"{ACCT}.r2.cloudflarestorage.com"
TOK = os.environ["CLOUDFLARE_API_TOKEN"]

def token_id():
    r = urllib.request.Request("https://api.cloudflare.com/client/v4/user/tokens/verify",
                               headers={"Authorization": f"Bearer {TOK}"})
    return json.load(urllib.request.urlopen(r))["result"]["id"]

def temp_creds(bucket, prefix, perm="object-read-write"):
    body = json.dumps({"bucket": bucket, "parentAccessKeyId": token_id(), "permission": perm,
                       "ttlSeconds": 900, "prefixes": [prefix]}).encode()
    r = urllib.request.Request(f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/r2/temp-access-credentials",
        data=body, method="POST", headers={"Authorization": f"Bearer {TOK}", "content-type": "application/json"})
    j = json.load(urllib.request.urlopen(r))["result"]
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
    url, h = sign(method, bucket, key, headers or {}, ph)
    r = urllib.request.Request(url, data=body if method == "PUT" else None, method=method, headers=h)
    try:
        resp = urllib.request.urlopen(r, timeout=120)
        return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

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
