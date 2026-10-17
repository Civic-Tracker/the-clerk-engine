import json
"""Clerk review: box reading, reconcile, clerk fixes, duplicates, major-discrepancy hold. Stdlib, no network."""
import os, sys, unittest
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
sys.path.insert(0, os.path.join(HERE, "..", "review"))
import form460 as F  # noqa: E402
import clerk_review as R  # noqa: E402

BOX_A = """SUBTOTAL $ 1,650.00
Schedule A Summary                                                    *Contributor Codes
1. Amount received this period - itemized monetary contributions.
   (Include all Schedule A subtotals.)                   $ 16,930.00   COM Recipient Committee
2. Amount received this period - unitemized monetary contributions of less than $100     $ 105.00
3. Total monetary contributions received this period.
   (Add Lines 1 and 2. Enter here and on the Summary Page, Column A, Line 1.)   TOTAL $ 17,035.00
FPPC Form 460 (Jan/2016)
"""

BOX_E = """Schedule E Summary
1. Itemized payments made this period. (Include all Schedule E subtotals.) ........ $37,338.80
2. Unitemized payments made this period of under $100. ........ $ 881.23
3. Total interest paid this period on loans. (Enter amount from Schedule B, Part 1, Column (e).) .... $0.00
4. Total payments made this period. (Add Lines 1, 2, and 3. Enter here and on the Summary Page, Column A, Line 6.) TOTAL $38,220.03
FPPC Form 460 (Jan/2016)
"""

BOX_E_DROPPED = BOX_E.replace("$ 881.23", "")          # OCR lost line 2's amount


class FakePages:
    """Pages stand-in: read A / B / C / D texts per page, word boxes (read T) and cell re-reads."""
    def __init__(self, a, b=None, c=None, d=None, words=None, cells=None):
        self.t = {"A": a, "B": b or a, "C": c or a, "D": d or {}}
        self.w = words or {}
        self.cell_text = cells or {}
        self.calls = defaultdict(int)

    def words(self, slug, sha, key, p):
        return self.w.get(p, [])

    def cached_words(self, sha, p):
        return True

    def cells(self, slug, sha, key, p, boxes):
        self.calls["cells"] += len(boxes)
        return [self.cell_text.get((p, int(b[1]) // 100), "") for b in boxes]

    def read_a(self, slug, sha, key, p):
        return self.t["A"].get(p, "")

    def read(self, which, slug, sha, key, p, box=False):
        self.calls[which] += 1
        return self.t[which].get(p, "")

    def cached(self, which, sha, p):
        return True

    def pages_of(self, slug, sha, key):
        return max(self.t["A"])


def filing(fid="cf_1", **kw):
    f = {"id": fid, "jurisdiction_id": "jurisdiction-testville", "filer_identity_id": "csi_a", "filer_name_raw": "Committee A",
         "form_number": "460", "amendment_number": 0, "filing_date": None, "period_start": "2018-01-01", "period_end": "2018-06-30",
         "cycle_year": 2018, "record_state": "ACTIVE", "evidence_r2_key": "raw/testville/x.pdf", "source_hash": "x" * 64,
         "raw_json": '{"source_url": "https://example.test/doc#pages=1-3"}'}
    f.update(kw)
    return f


def row(i, sched, amt, page=2, date="2018-02-01", name=None, state="OCR_UNREVIEWED"):
    return {"id": f"ct_{i}", "jurisdiction_id": "jurisdiction-testville", "filing_id": "cf_1", "schedule_code": sched,
            "transaction_kind": "CONTRIBUTION", "amount_minor": amt, "transaction_date": date, "record_state": state,
            "provenance": "ocr", "counterparty_name_raw": name or f"Donor {i}", "cycle_year": 2018,
            "raw_json": f'{{"page": {page}}}'}


class TestForm460(unittest.TestCase):
    def test_money_tokens(self):
        self.assertEqual(F.cents_tokens("$16,930.00 and 105.00 and -0-"), [1693000, 10500, 0])
        self.assertEqual(F.cents_tokens("TOTAL $3,200"), [320000])

    def test_box_a(self):
        r = F.read_box(BOX_A, "A")
        self.assertEqual(r["lines"], [1693000, 10500, 1703500])
        self.assertTrue(r["arith"])

    def test_box_e(self):
        r = F.read_box(BOX_E, "E")
        self.assertEqual(r["lines"], [3733880, 88123, 0, 3822003])
        self.assertTrue(r["arith"])

    def test_box_e_dropped_line(self):
        r = F.read_box(BOX_E_DROPPED, "E")
        self.assertEqual(r["lines"][0], 3733880)        # line 1 still read
        self.assertFalse(r["arith"])

    def test_no_box(self):
        self.assertIsNone(F.read_box("Schedule A (Continuation Sheet)\nfoo 100.00", "A"))

    def test_misread_variants(self):
        v = F.misread_variants(45000)
        self.assertIn(4500, v)          # decimal slip
        self.assertIn(450000, v)
        self.assertIn(48000, v)         # 5 -> 8 confusable
        self.assertIn(54000, v)         # transposition
        self.assertNotIn(45000, v)

    def test_text_has_amount(self):
        self.assertTrue(F.text_has_amount("Jane Doe  1,250.00  1,250.00", 125000))
        self.assertFalse(F.text_has_amount("Jane Doe  1,520.00", 125000))


class TestReview(unittest.TestCase):
    def pages(self, box_page_text, b=None, extra=None):
        a = {1: "Campaign Disclosure Statement Summary Page", 2: box_page_text, 3: "Schedule E Payments Made"}
        a.update(extra or {})
        bb = dict(a)
        if b is not None:
            bb[2] = b
        return FakePages(a, bb)

    def test_pass_when_rows_reconcile(self):
        rows = [row(1, "460A", 1000000), row(2, "460A", 693000)]
        r = R.review_filing(filing(), rows, self.pages(BOX_A), deadline=9e18)
        self.assertEqual(r["fail"], [])
        self.assertEqual(r["checks"]["A"]["itemized"], 1693000)

    def test_flag_when_reads_disagree(self):
        b = BOX_A.replace("16,930.00", "18,930.00").replace("17,035.00", "19,035.00")
        c = BOX_A.replace("16,930.00", "16,980.00")
        p = self.pages(BOX_A, b=b)
        p.t["C"] = dict(p.t["A"]); p.t["C"][2] = c
        p.t["D"] = {2: BOX_A.replace("16,930.00", "16,830.00")}
        rows = [row(1, "460A", 1693000)]
        r = R.review_filing(filing(), rows, p, deadline=9e18)
        self.assertIn("A:summary_reads_disagree", r["fail"])

    def test_read_d_settles_three_way_disagreement(self):
        b = BOX_A.replace("16,930.00", "18,930.00").replace("17,035.00", "19,035.00")
        c = BOX_A.replace("16,930.00", "16,980.00")
        p = self.pages(BOX_A, b=b)
        p.t["C"] = dict(p.t["A"]); p.t["C"][2] = c
        p.t["D"] = {2: BOX_A}                              # D agrees with A exactly: 2 reads, no rival pair
        r = R.review_filing(filing(), [row(1, "460A", 1693000)], p, deadline=9e18)
        self.assertEqual(r["fail"], [])
        self.assertEqual(r["checks"]["A"]["reads"][0]["settled_by"], "read_D")

    def test_vote(self):
        self.assertEqual(R.vote([1, 1, 2]), 1)
        self.assertIsNone(R.vote([1, 2, 3]))
        self.assertIsNone(R.vote([1, 1, 2, 2]))           # two pairs disagree: nothing is accepted
        self.assertIsNone(R.vote([None, None, 5]))

    def test_tiebreak_read_c(self):
        b = BOX_A.replace("16,930.00", "18,930.00")
        p = self.pages(BOX_A, b=b)          # read C (default = A text) agrees with A -> 2 of 3
        r = R.review_filing(filing(), [row(1, "460A", 1693000)], p, deadline=9e18)
        self.assertEqual(r["fail"], [])
        self.assertEqual(p.calls["C"], 1)

    def test_flag_when_rows_short_and_no_evidence(self):
        rows = [row(1, "460A", 1000000), row(2, "460A", 600000)]
        r = R.review_filing(filing(), rows, self.pages(BOX_A), deadline=9e18)
        self.assertIn("A:rows_do_not_reconcile", r["fail"])
        self.assertEqual(r["gap"]["A"], -93000)

    def test_fix_decimal_slip_with_evidence(self):
        page2 = BOX_A.replace("SUBTOTAL", "Jane Doe IND  6,930.00\nSUBTOTAL")
        rows = [row(1, "460A", 1000000), row(2, "460A", 69300)]       # 6,930.00 read as 693.00
        r = R.review_filing(filing(), rows, self.pages(page2), deadline=9e18)
        self.assertEqual(r["fail"], [])
        self.assertEqual(r["corrections"][0]["rule"], "fix_misread_amount")
        self.assertEqual((r["corrections"][0]["before"], r["corrections"][0]["after"]), (69300, 693000))

    def test_no_fix_without_printed_value(self):
        rows = [row(1, "460A", 1000000), row(2, "460A", 69300)]       # 6,930.00 not printed in any read
        r = R.review_filing(filing(), rows, self.pages(BOX_A), deadline=9e18)
        self.assertIn("A:rows_do_not_reconcile", r["fail"])
        self.assertEqual(r["corrections"], [])

    def test_fill_unread_amount(self):
        page2 = BOX_A.replace("SUBTOTAL", "John Roe IND 450.00\nSUBTOTAL")
        rows = [row(1, "460A", 1648000), row(2, "460A", None)]
        r = R.review_filing(filing(), rows, self.pages(page2), deadline=9e18)
        self.assertEqual(r["fail"], [])
        self.assertEqual(r["corrections"][0]["rule"], "fill_unread_amount")
        self.assertEqual(r["corrections"][0]["after"], 45000)

    def test_drop_doubled_row(self):
        rows = [row(1, "460A", 1693000 - 50000), row(2, "460A", 50000, name="Pat Lee"), row(3, "460A", 50000, name="Pat Lee")]
        r = R.review_filing(filing(), rows, self.pages(BOX_A), deadline=9e18)
        self.assertEqual(r["fail"], [])
        self.assertEqual(r["drop"][0]["rule"], "drop_duplicate_row")

    def test_missing_schedule_is_zero(self):
        r = R.review_filing(filing(), [], self.pages("Schedule B Loans"), deadline=9e18)
        self.assertEqual(r["fail"], [])
        self.assertEqual(r["checks"]["A"]["itemized"], 0)

    def test_rows_without_box_flag(self):
        r = R.review_filing(filing(), [row(1, "460E", 5000)], self.pages("nothing here"), deadline=9e18)
        self.assertIn("E:no_summary_box", r["fail"])

    def test_photocopied_blank_boxes_ignored(self):
        blank = BOX_A.replace("16,930.00", "").replace("105.00", "").replace("17,035.00", "")
        p = FakePages({1: blank, 2: BOX_A, 3: blank})
        r = R.review_filing(filing(), [row(1, "460A", 1693000)], p, deadline=9e18)
        self.assertEqual(r["fail"], [])


class TestNames(unittest.TestCase):
    def test_form_text(self):
        for n in ["OF BUSINESS)", "NAME AND ADDRESS OF PAYEE", "El", "(", "Schedule A (Continuation Sheet)"]:
            self.assertTrue(R.form_text_name(n), n)
        for n in ["", None, "Michael Abrams", "SUTTON LAW FIRM", "Ng, Al"]:
            self.assertFalse(R.form_text_name(n), n)

    def test_form_text_name_cleared_when_rows_reconcile(self):
        rows = [row(1, "460A", 1693000, name="OF BUSINESS)")]
        a = {1: "Summary Page", 2: BOX_A, 3: ""}
        r = R.review_filing(filing(), rows, FakePages(a), deadline=9e18)
        self.assertEqual(r["fail"], [])
        self.assertEqual([(c["rule"], c["after"]) for c in r["corrections"]], [("clear_form_text_name", "")])

    def test_form_text_row_dropped_only_if_then_reconciles(self):
        rows = [row(1, "460A", 1693000), row(2, "460A", 50000, name="AMOUNT RECEIVED THIS PERIOD")]
        a = {1: "Summary Page", 2: BOX_A, 3: ""}
        r = R.review_filing(filing(), rows, FakePages(a), deadline=9e18)
        self.assertEqual(r["fail"], [])
        self.assertEqual([(c["rule"], c["transaction_id"], c["after"]) for c in r["corrections"]],
                         [("drop_form_text_row", "ct_2", "OCR_REJECTED")])

    def test_form_text_row_kept_flagged_when_no_reconcile(self):
        rows = [row(1, "460A", 1000000), row(2, "460A", 50000, name="OF BUSINESS)")]
        a = {1: "Summary Page", 2: BOX_A, 3: ""}
        r = R.review_filing(filing(), rows, FakePages(a), deadline=9e18)
        self.assertIn("A:row_name_is_form_text", r["fail"])
        self.assertIn("A:rows_do_not_reconcile", r["fail"])


class TestDuplicates(unittest.TestCase):
    def setUp(self):
        self.rows = lambda fid, n, base=0: [dict(row(i + base, "460A", 10000 + i + base * 100, date=f"2018-02-{(i % 27) + 1:02d}"), filing_id=fid) for i in range(n)]

    def run_dups(self, filings, rows, efile=None):
        by = defaultdict(list)
        for r in rows:
            by[r["filing_id"]].append(r)
        return R.dup_groups(filings, by, efile or {})

    def test_identical_copy(self):
        fs = {"cf_1": filing("cf_1"), "cf_2": filing("cf_2")}
        d = self.run_dups(fs, self.rows("cf_1", 8) + self.rows("cf_2", 8))
        self.assertEqual(d, {"cf_2": ("duplicate_copy", "cf_1")})

    def test_amendment_wins(self):
        fs = {"cf_1": filing("cf_1"), "cf_2": filing("cf_2", amendment_number=1)}
        d = self.run_dups(fs, self.rows("cf_1", 8) + self.rows("cf_2", 9))
        self.assertEqual(d, {"cf_1": ("superseded_by_amendment", "cf_2")})

    def test_unknown_order_flags_both(self):
        fs = {"cf_1": filing("cf_1"), "cf_2": filing("cf_2")}
        d = self.run_dups(fs, self.rows("cf_1", 8) + self.rows("cf_2", 9))
        self.assertEqual({k: v[0] for k, v in d.items()}, {"cf_1": "amendment_order_unknown", "cf_2": "amendment_order_unknown"})

    def test_overlapping_periods_same_committee(self):
        fs = {"cf_1": filing("cf_1"), "cf_2": filing("cf_2", period_start="2018-01-01", period_end="2018-02-24")}
        d = self.run_dups(fs, self.rows("cf_1", 8) + self.rows("cf_2", 3, base=50))
        self.assertEqual({k: v[0] for k, v in d.items()}, {"cf_1": "overlapping_periods", "cf_2": "overlapping_periods"})

    def test_different_committees_differnt_rows_ok(self):
        fs = {"cf_1": filing("cf_1"), "cf_2": filing("cf_2", filer_identity_id="csi_b", filer_name_raw="Committee B", source_hash="y" * 64)}
        d = self.run_dups(fs, self.rows("cf_1", 8) + self.rows("cf_2", 8, base=100))
        self.assertEqual(d, {})

    def test_efile_duplicate(self):
        fs = {"cf_1": filing("cf_1")}
        rows = self.rows("cf_1", 6)
        ek = {"jurisdiction-testville": {(r["schedule_code"], r["amount_minor"], r["transaction_date"]) for r in rows}}
        d = self.run_dups(fs, rows, ek)
        self.assertEqual(d["cf_1"][0], "duplicate_of_efile")


    def test_same_document_pages_is_one_statement(self):
        fs = {"cf_1": filing("cf_1"), "cf_2": filing("cf_2", filer_identity_id="csi_b", filer_name_raw="Committee B")}
        d = self.run_dups(fs, self.rows("cf_1", 8) + self.rows("cf_2", 8, base=100))
        self.assertEqual({k: v[0] for k, v in d.items()}, {"cf_1": "amendment_order_unknown", "cf_2": "amendment_order_unknown"})

    def test_contained_period_superseded_when_amounts_match(self):
        fs = {"cf_1": filing("cf_1", source_hash="a" * 64), "cf_2": filing("cf_2", period_end="2018-02-24", source_hash="b" * 64)}
        big = self.rows("cf_1", 8)
        small = [dict(r, id=r["id"] + "s", filing_id="cf_2") for r in big[:3]]
        d = self.run_dups(fs, big + small)
        self.assertEqual(d, {"cf_2": ("superseded_by_containing_period", "cf_1")})

    def test_near_period_is_same_period(self):
        self.assertTrue(R.near_period(("2016-02-28", "2016-03-26"), ("2016-02-29", "2016-03-27")))
        self.assertFalse(R.near_period(("2016-01-01", "2016-06-30"), ("2016-01-01", "2016-04-12")))

    def test_tie_goes_to_copy_check(self):
        fs = {"cf_1": filing("cf_1", source_hash="a" * 64), "cf_2": filing("cf_2", source_hash="b" * 64)}
        by = defaultdict(list)
        for r in self.rows("cf_1", 8) + self.rows("cf_2", 9):
            by[r["filing_id"]].append(r)
        pend = {}
        d = R.dup_groups(fs, by, {}, pending=pend)
        self.assertEqual(d, {})
        self.assertEqual(pend, {"cf_1": ["cf_1", "cf_2"], "cf_2": ["cf_1", "cf_2"]})
        ck = {s: {"itemized": v} for s, v in zip("ACE", (100, 0, 50))}
        res = {"cf_1": {"verdict": "FLAG", "fail": ["A:rows_do_not_reconcile"], "checks": dict(ck), "corrections": [], "drop": []},
               "cf_2": {"verdict": "PASS", "fail": [], "checks": dict(ck), "corrections": [], "drop": []}}
        R.resolve_copies(pend, res, {})
        self.assertEqual((res["cf_1"]["verdict"], res["cf_1"]["fail"][0]), ("FLAG", "same_statement_copy"))
        self.assertEqual(res["cf_2"]["verdict"], "PASS")
        res["cf_1"]["checks"] = {s: {"itemized": v} for s, v in zip("ACE", (101, 0, 50))}
        res["cf_2"] = {"verdict": "PASS", "fail": [], "checks": dict(ck), "corrections": [], "drop": []}
        R.resolve_copies(pend, res, {})
        self.assertEqual((res["cf_2"]["verdict"], res["cf_2"]["fail"][0]), ("FLAG", "amendment_order_unknown"))


    def test_two_scans_few_undated_rows_are_one_statement(self):
        # Weissman case: 3 undated Schedule E rows, filer read as "Weissman" and "Weissman, Andrew" (two identities)
        fs = {"cf_1": filing("cf_1", filer_identity_id="csi_a", filer_name_raw="Weissman", filing_date="2014-01-09", source_hash="a" * 64,
                             period_start="2013-07-01", period_end="2013-12-31"),
              "cf_2": filing("cf_2", filer_identity_id="csi_b", filer_name_raw="Weissman, Andrew", filing_date="2014-01-09", source_hash="b" * 64,
                             period_start="2013-07-01", period_end="2013-12-31")}
        rows = [dict(row(f"{fid}{i}", "460E", amt, date=None), filing_id=fid, record_state="ACTIVE")
                for fid in ("cf_1", "cf_2") for i, amt in enumerate((17500, 18000, 50000))]
        self.assertEqual(self.run_dups(fs, rows), {"cf_2": ("duplicate_copy", "cf_1")})
        fs["cf_2"]["filing_date"] = "2014-02-01"           # filed on another day: not this rule
        self.assertEqual(self.run_dups(fs, rows), {})
        fs["cf_2"]["filing_date"] = "2014-01-09"
        rows[-1] = dict(rows[-1], amount_minor=50100)       # one row differs: not a copy
        self.assertEqual(self.run_dups(fs, rows), {})


class TestMisc(unittest.TestCase):
    def test_page_range(self):
        self.assertEqual(R.page_range(filing(), lambda: 99), (1, 3))
        self.assertEqual(R.page_range(filing(raw_json="{}"), lambda: 7), (1, 7))

    def test_rows_fp_stable(self):
        a = [row(1, "460A", 100), row(2, "460E", 200)]
        self.assertEqual(R.rows_fp(a), R.rows_fp(list(reversed(a))))
        self.assertNotEqual(R.rows_fp(a), R.rows_fp([row(1, "460A", 101), row(2, "460E", 200)]))

    def test_held_corrected_pass_is_reviewed_again(self):
        # a held batch writes the verdict but not its fixes: a CORRECTED_PASS never applied must be re-derived, not re-applied
        fs = {f: filing(f) for f in ("cf_1", "cf_2", "cf_3", "cf_4")}
        rows_by = {f: [dict(row(f + "a", "460A", 100), filing_id=f)] for f in fs}
        fp = {f: R.rows_fp(R.live_rows(rows_by[f])) for f in fs}
        prior = {"cf_1": {"verdict": "CORRECTED_PASS", "applied": 0, "rows_fp": fp["cf_1"], "reason": None, "checks": "{}"},
                 "cf_2": {"verdict": "PASS", "applied": 0, "rows_fp": fp["cf_2"], "reason": None, "checks": "{}"},
                 "cf_3": {"verdict": "CORRECTED_PASS", "applied": 1, "rows_fp": fp["cf_3"], "reason": None, "checks": "{}"},
                 "cf_4": {"verdict": "FLAG", "applied": 0, "rows_fp": fp["cf_4"], "reason": "A:rows_do_not_reconcile",
                          "checks": json.dumps({"robot_v": R.ROBOT_V})}}
        todo, reapply, unflip = R.plan_work(fs, rows_by, {}, {}, prior)
        self.assertEqual((todo, reapply, unflip), (["cf_1"], ["cf_2", "cf_3"], []))

    def test_staging_guard(self):
        import d1
        os.environ.setdefault("CLOUDFLARE_API_TOKEN", "x")
        with self.assertRaises(SystemExit):
            d1.D1(db="726f7c4e-4fce-4a10-8a31-d804ee000b79")


if __name__ == "__main__":
    unittest.main()
