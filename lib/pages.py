"""Page text for the clerk review: the ingest's first read (read A) and fresh independent OCR reads (B, C).

Read A, in order: a local derived-text dir (<dir>/<sha256>/p<N>.txt, the ingest store layout), a staging R2
text bundle (_index/text/<slug>/<sha256>.json.gz = {"<page>": "<text>"}), else the PDF's own text layer
(pdftotext) or tesseract psm 6 at 300 dpi (the ingest's settings).
Read B: tesseract psm 4 at 300 dpi on a grayscale render (different page segmentation from read A).
Read C (tie-break only): tesseract psm 6 at 400 dpi.
PDFs come from a local object store (<dir>/<sha[:2]>/<sha>.pdf) or, read-only, from the staging R2 key.
Every OCR result is cached on disk under --cache-dir, so a time-boxed run resumes where it stopped.
"""
import gzip, json, os, re, subprocess, tempfile

STAGING_BUCKET = "civic-tracker-bakeoff-evidence"

READS = {"B": ("300", "4"), "C": ("400", "6")}


def have(cmd):
    from shutil import which
    return which(cmd) is not None


class Pages:
    def __init__(self, cache_dir, text_dirs=None, pdf_dirs=None, r2=True):
        self.cache = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.text_dirs = text_dirs or {}   # slug -> derived/text dir
        self.pdf_dirs = pdf_dirs or {}     # slug -> raw/objects dir
        self.r2 = r2
        self._bundles = {}
        self.stats = {"ocr_pages": 0, "r2_get": 0}

    # ---- PDFs
    def pdf_path(self, slug, sha, r2_key):
        d = self.pdf_dirs.get(slug)
        if d:
            p = os.path.join(d, sha[:2], f"{sha}.pdf")
            if os.path.exists(p):
                return p
        p = os.path.join(self.cache, "pdf", f"{sha}.pdf")
        if os.path.exists(p):
            return p
        if not (self.r2 and r2_key):
            return None
        import r2s3
        prefix = r2_key.rsplit("/", 1)[0] + "/"
        r2s3.use(STAGING_BUCKET, prefix, perm="object-read-only")
        s, _, body = r2s3.req("GET", STAGING_BUCKET, r2_key)
        self.stats["r2_get"] += 1
        if s != 200 or not body.startswith(b"%PDF"):
            return None
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(body)
        return p

    def page_count(self, pdf):
        out = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout
        for ln in out.splitlines():
            if ln.startswith("Pages:"):
                return int(ln.split()[1])
        return 0

    # ---- read A
    def _bundle(self, slug, sha):
        k = (slug, sha)
        if k in self._bundles:
            return self._bundles[k]
        b = None
        d = self.text_dirs.get(slug)
        if d and os.path.isdir(os.path.join(d, sha)):
            b = {}
            for fn in os.listdir(os.path.join(d, sha)):
                if fn.startswith("p") and fn.endswith(".txt"):
                    with open(os.path.join(d, sha, fn), encoding="utf-8", errors="replace") as f:
                        b[int(fn[1:-4])] = f.read()
        cp = os.path.join(self.cache, "bundles", f"{sha}.json.gz")
        if b is None:
            if not os.path.exists(cp) and self.r2:
                try:
                    import r2s3
                    key = f"_index/text/{slug}/{sha}.json.gz"
                    r2s3.use(STAGING_BUCKET, f"_index/text/{slug}/", perm="object-read-only")
                    s, _, body = r2s3.req("GET", STAGING_BUCKET, key)
                    self.stats["r2_get"] += 1
                    if s == 200:
                        os.makedirs(os.path.dirname(cp), exist_ok=True)
                        with open(cp, "wb") as f:
                            f.write(body)
                except Exception:
                    pass
            if os.path.exists(cp):
                with gzip.open(cp, "rt", encoding="utf-8") as f:
                    b = {int(k2): v for k2, v in json.load(f).items()}
        self._bundles[k] = b
        return b

    def read_a(self, slug, sha, r2_key, page):
        b = self._bundle(slug, sha)
        if b is not None and page in b:
            return b[page]
        cp = os.path.join(self.cache, "A", sha, f"p{page}.txt")
        if os.path.exists(cp):
            return open(cp, encoding="utf-8").read()
        pdf = self.pdf_path(slug, sha, r2_key)
        if not pdf:
            return None
        t = subprocess.run(["pdftotext", "-layout", "-f", str(page), "-l", str(page), pdf, "-"], capture_output=True, text=True).stdout
        if len(t.strip()) < 40:
            t = self._ocr(pdf, page, "300", "6")
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        with open(cp, "w", encoding="utf-8") as f:
            f.write(t)
        return t

    def pages_of(self, slug, sha, r2_key):
        b = self._bundle(slug, sha)
        if b:
            return max(b)
        pdf = self.pdf_path(slug, sha, r2_key)
        return self.page_count(pdf) if pdf else 0

    # ---- reads B / C
    def read(self, which, slug, sha, r2_key, page, box=False):
        """Fresh OCR read of a page. box=True reads the lower part first (the schedule summary box) and
        falls back to the whole page when no box heading is found there."""
        dpi, psm = READS[which]
        cp = os.path.join(self.cache, which, sha, f"p{page}.txt")
        if os.path.exists(cp):
            return open(cp, encoding="utf-8").read()
        pdf = self.pdf_path(slug, sha, r2_key)
        if not pdf:
            return None
        t = self._ocr(pdf, page, dpi, psm, bottom=0.55) if box else ""
        if not box or not re.search(r"summ?ary", t, re.I):
            t = self._ocr(pdf, page, dpi, psm)
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        with open(cp, "w", encoding="utf-8") as f:
            f.write(t)
        return t

    def cached(self, which, sha, page):
        return os.path.exists(os.path.join(self.cache, which, sha, f"p{page}.txt"))

    def page_height_px(self, pdf, page, dpi):
        out = subprocess.run(["pdfinfo", "-f", str(page), "-l", str(page), pdf], capture_output=True, text=True).stdout
        for ln in out.splitlines():
            if ln.startswith(f"Page") and "size:" in ln:
                try:
                    parts = ln.split("size:")[1].split()
                    return int(float(parts[2]) * int(dpi) / 72)
                except (IndexError, ValueError):
                    return None
        return None

    def _ocr(self, pdf, page, dpi, psm, bottom=None):
        """OCR one page; bottom=0.55 reads only the lower 55% (where a schedule's summary box sits)."""
        self.stats["ocr_pages"] += 1
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, "pg")
            crop = []
            if bottom:
                h = self.page_height_px(pdf, page, dpi)
                if h:
                    crop = ["-y", str(int(h * (1 - bottom))), "-H", str(int(h * bottom) + 2), "-W", "100000", "-x", "0"]
            subprocess.run(["pdftoppm", "-r", dpi, "-gray", "-f", str(page), "-l", str(page), "-png", "-singlefile", *crop, pdf, base],
                           capture_output=True, timeout=300)
            if not os.path.exists(base + ".png"):
                return ""
            r = subprocess.run(["tesseract", base + ".png", "-", "--psm", psm], capture_output=True, text=True, timeout=300)
            return r.stdout
