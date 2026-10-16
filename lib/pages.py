"""Page text for the clerk review: the ingest's first read (read A) and fresh independent OCR reads (B, C).

Read A, in order: a local derived-text dir (<dir>/<sha256>/p<N>.txt, the ingest store layout), a staging R2
text bundle (_index/text/<slug>/<sha256>.json.gz = {"<page>": "<text>"}), else the PDF's own text layer
(pdftotext) or tesseract psm 6 at 300 dpi (the ingest's settings).
Read B: tesseract psm 4 at 300 dpi on a grayscale render (different page segmentation from read A).
Read C (tie-break only): tesseract psm 6 at 400 dpi.
Read D (third read for a summary box the first three could not settle): tesseract psm 4 at 400 dpi on a
thresholded (black/white) render - a different engine path from B and C.
Read T (row rebuild): word boxes for a whole schedule page, from the PDF's own text layer when it has one, else
tesseract psm 6 at 400 dpi as TSV. Cached as <cache>/T/<sha>/p<N>.tsv (left, top, width, height, conf, text).
PDFs come from a local object store (<dir>/<sha[:2]>/<sha>.pdf) or, read-only, from the staging R2 key.
Every OCR result is cached on disk under --cache-dir, so a time-boxed run resumes where it stopped.
"""
import gzip, json, os, re, subprocess, tempfile, threading

STAGING_BUCKET = "civic-tracker-bakeoff-evidence"

READS = {"B": ("300", "4"), "C": ("400", "6"), "D": ("400", "4")}
THRESH = {"D": "55%"}
T_DPI = 400


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
        th = THRESH.get(which)
        t = self._ocr(pdf, page, dpi, psm, bottom=0.55, threshold=th) if box else ""
        if not box or not re.search(r"summ?ary", t, re.I):
            t = self._ocr(pdf, page, dpi, psm, threshold=th)
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

    # ---- read T (word boxes)
    def words(self, slug, sha, r2_key, page):
        """Word boxes of a page at T_DPI: [(left, top, width, height, conf, text)], or None when no PDF."""
        cp = os.path.join(self.cache, "T", sha, f"p{page}.tsv")
        if not os.path.exists(cp):
            pdf = self.pdf_path(slug, sha, r2_key)
            if not pdf:
                return None
            ws = self._text_layer_words(pdf, page)
            if ws is None:
                ws = self._ocr_words(pdf, page)
            os.makedirs(os.path.dirname(cp), exist_ok=True)
            with open(cp + f".{os.getpid()}.{threading.get_ident()}.tmp", "w", encoding="utf-8") as f:
                for w in ws:
                    f.write("\t".join(str(x) for x in w[:5]) + "\t" + w[5].replace("\t", " ").replace("\n", " ") + "\n")
            os.replace(cp + f".{os.getpid()}.{threading.get_ident()}.tmp", cp)
        out = []
        with open(cp, encoding="utf-8") as f:
            for ln in f:
                p = ln.rstrip("\n").split("\t")
                if len(p) >= 6 and p[5].strip():
                    out.append((int(p[0]), int(p[1]), int(p[2]), int(p[3]), float(p[4]), p[5]))
        return out

    def cells(self, slug, sha, r2_key, page, boxes):
        """Re-read small boxes of a page (x0, y0, x1, y1 at T_DPI) one by one as a single text line, digits and
        money punctuation only (tesseract psm 7). Cached per page in <cache>/T/<sha>/p<N>.cells.json."""
        cp = os.path.join(self.cache, "T", sha, f"p{page}.cells.json")
        have_ = {}
        if os.path.exists(cp):
            with open(cp, encoding="utf-8") as f:
                have_ = json.load(f)
        keys = [",".join(str(int(v)) for v in b) for b in boxes]
        todo = [(k, b) for k, b in zip(keys, boxes) if k not in have_]
        if todo:
            pdf = self.pdf_path(slug, sha, r2_key)
            if pdf:
                with tempfile.TemporaryDirectory() as td:
                    base = os.path.join(td, "pg")
                    subprocess.run(["pdftoppm", "-r", str(T_DPI), "-gray", "-f", str(page), "-l", str(page), "-png", "-singlefile", pdf, base],
                                   capture_output=True, timeout=300)
                    for k, b in todo:
                        x0, y0, x1, y1 = (max(0, int(v)) for v in b)
                        cp_png = os.path.join(td, "c.png")
                        subprocess.run(["convert", base + ".png", "-crop", f"{x1 - x0}x{y1 - y0}+{x0}+{y0}", "+repage",
                                        "-bordercolor", "white", "-border", "20", cp_png], capture_output=True, timeout=60)
                        self.stats["ocr_cells"] = self.stats.get("ocr_cells", 0) + 1
                        r = subprocess.run(["tesseract", cp_png, "-", "--psm", "7", "-c", "tessedit_char_whitelist=0123456789.,$"],
                                           capture_output=True, text=True, timeout=60)
                        have_[k] = r.stdout.strip()
                os.makedirs(os.path.dirname(cp), exist_ok=True)
                with open(cp + f".{os.getpid()}.{threading.get_ident()}.tmp", "w", encoding="utf-8") as f:
                    json.dump(have_, f)
                os.replace(cp + f".{os.getpid()}.{threading.get_ident()}.tmp", cp)
        return [have_.get(k) for k in keys]

    def cached_words(self, sha, page):
        return os.path.exists(os.path.join(self.cache, "T", sha, f"p{page}.tsv"))

    def _text_layer_words(self, pdf, page):
        """Words of the PDF's own text layer scaled to T_DPI, or None when the page has (almost) no text layer."""
        r = subprocess.run(["pdftotext", "-bbox", "-f", str(page), "-l", str(page), pdf, "-"], capture_output=True, text=True)
        ws = []
        k = T_DPI / 72.0
        for m in re.finditer(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', r.stdout):
            x0, y0, x1, y1 = (float(m.group(i)) * k for i in range(1, 5))
            t = m.group(5).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
            ws.append((int(x0), int(y0), int(x1 - x0), int(y1 - y0), 99.0, t))
        return ws if len(ws) >= 30 else None

    def _ocr_words(self, pdf, page):
        import csv, io
        self.stats["ocr_pages"] += 1
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, "pg")
            subprocess.run(["pdftoppm", "-r", str(T_DPI), "-gray", "-f", str(page), "-l", str(page), "-png", "-singlefile", pdf, base],
                           capture_output=True, timeout=300)
            if not os.path.exists(base + ".png"):
                return []
            r = subprocess.run(["tesseract", base + ".png", "-", "--psm", "6", "tsv"], capture_output=True, text=True, timeout=300)
        ws = []
        for row in csv.DictReader(io.StringIO(r.stdout), delimiter="\t", quoting=csv.QUOTE_NONE):
            if row.get("level") == "5" and (row.get("text") or "").strip():
                try:
                    ws.append((int(row["left"]), int(row["top"]), int(row["width"]), int(row["height"]), float(row["conf"]), row["text"]))
                except (TypeError, ValueError):
                    pass
        return ws

    def _ocr(self, pdf, page, dpi, psm, bottom=None, threshold=None):
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
            if threshold:
                subprocess.run(["convert", base + ".png", "-threshold", threshold, base + ".png"], capture_output=True, timeout=120)
            r = subprocess.run(["tesseract", base + ".png", "-", "--psm", psm], capture_output=True, text=True, timeout=300)
            return r.stdout
