"""Staging-only D1 client (stdlib). Refuses any database but the staging one."""
import json, os, urllib.request
import net
from net import AuthError

ACCOUNT = "1237f978938188ed96dc859fdd2d40d0"
STAGING_D1 = "5caedb62-49c2-437b-a87a-b85923911ad3"


class D1:
    def __init__(self, db=STAGING_D1, acct=ACCOUNT):
        if db != STAGING_D1:
            raise SystemExit(f"refusing: D1 {db} is not the staging database")
        self.base = f"https://api.cloudflare.com/client/v4/accounts/{acct}/d1/database/{db}"
        self.tok = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not self.tok:
            raise AuthError("CLOUDFLARE_API_TOKEN is not set")

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        hd = {"Authorization": f"Bearer {self.tok}"}
        if data is not None:
            hd["content-type"] = "application/json"
        s, _, b = net.request(urllib.request.Request(self.base + path, data=data, method=method, headers=hd), timeout=120)
        try:
            j = json.loads(b or b"{}")
        except ValueError:
            j = {}
        if s in (401, 403):
            raise AuthError(f"D1 HTTP {s} (token needs Account > D1 > Edit): {json.dumps(j.get('errors'))[:160]}")
        if s != 200 or not j.get("success"):
            raise RuntimeError(f"D1 HTTP {s}: {json.dumps(j.get('errors'))[:300]}")
        return j["result"]

    def q(self, sql, params=None):
        """One statement -> list of row dicts."""
        r = self._call("POST", "/query", {"sql": sql, "params": params} if params else {"sql": sql})
        return r[0].get("results", []) if r else []

    def all(self, sql, page=2000):
        """SELECT paged with LIMIT/OFFSET (sql must have a stable ORDER BY)."""
        out, off = [], 0
        while True:
            rs = self.q(f"{sql} LIMIT {page} OFFSET {off}")
            out += rs
            off += page
            if len(rs) < page:
                return out

    def exec_many(self, stmts, max_bytes=90_000):
        """Run literal statements in batches (one request per batch). Returns number of requests."""
        n, buf, size = 0, [], 0
        for s in stmts:
            if buf and size + len(s) > max_bytes:
                self._call("POST", "/query", {"sql": ";\n".join(buf)}); n += 1; buf, size = [], 0
            buf.append(s); size += len(s) + 2
        if buf:
            self._call("POST", "/query", {"sql": ";\n".join(buf)}); n += 1
        return n

    def bookmark(self):
        """Current Time Travel bookmark (restore point taken before any write)."""
        return self._call("GET", "/time_travel/bookmark")["bookmark"]


def lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"
