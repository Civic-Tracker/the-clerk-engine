"""Robot 1: table-aware row rebuild from word boxes. Stdlib, no network, no OCR."""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
sys.path.insert(0, os.path.join(HERE, "..", "review"))
import tablerows as T  # noqa: E402
import clerk_review as R  # noqa: E402
from test_clerk_review import FakePages, BOX_A, filing, row  # noqa: E402


def w(x, y, t, wd=150, h=40):
    return (x, y, wd, h, 95.0, t)


def sched_a_page(rows, subtotal=None, heading="Schedule A (Continuation Sheet)"):
    """Words of a Form 460 Schedule A page at 400 dpi: heading, column headings, one line per row, SUBTOTAL."""
    ws = [w(220, 250, heading.split()[0]), w(560, 250, heading.split()[1])] + [w(3700 + 40 * i, 250, t) for i, t in enumerate(heading.split()[2:])]
    ws += [w(330, 880, "DATE"), w(700, 880, "FULL"), w(810, 880, "NAME,"), w(1840, 880, "CONTRIBUTOR"),
           w(2920, 880, "AMOUNT"), w(3250, 880, "CUMULATIVE"), w(3810, 880, "PER"), w(2860, 912, "RECEIVED"), w(2930, 980, "PERIOD")]
    y = 1100
    for date, name, amt, cum in rows:
        ws += [w(255, y, date)] + [w(590 + 160 * i, y, n) for i, n in enumerate(name.split())] + [w(1850, y, "IND"), w(2830, y, amt), w(3255, y, cum)]
        ws += [w(590, y + 70, "Culver"), w(740, y + 70, "City,"), w(860, y + 70, "CA"), w(950, y + 70, "90230")]
        y += 300
    if subtotal:
        ws += [w(2500, y, "SUBTOTAL"), w(2700, y, "$"), w(2830, y, subtotal)]
    ws += [w(3630, 3070, "FPPC"), w(3740, 3070, "Form"), w(3850, 3070, "460"), w(4100, 3070, "x")]
    return ws


class TestMoney(unittest.TestCase):
    def test_money(self):
        self.assertEqual(T.money("$1,480.05"), 148005)
        self.assertEqual(T.money("500.00"), 50000)
        self.assertEqual(T.money("500"), 50000)
        self.assertIsNone(T.money("500M0"))
        self.assertIsNone(T.money("12/26/20"))
        self.assertIsNone(T.money("ND"))

    def test_dates(self):
        self.assertEqual(T.parse_date("11/30/2023"), "2023-11-30")
        self.assertEqual(T.parse_date("10/21/20-"), "2020-10-21")
        self.assertIsNone(T.parse_date("13/40/2020"))


class TestRebuild(unittest.TestCase):
    def test_rows_and_subtotal(self):
        ws = sched_a_page([("11/30/2023", "Nancy Fard", "200.00", "700.00"), ("12/1/2023", "Nadar Khosravi", "500.00", "500.00")], "700.00")
        self.assertEqual(T.page_schedule(ws), "A")
        r = T.rebuild_page(ws, "A")
        self.assertEqual([(x["amount"], x["name"], x["date"]) for x in r["rows"]],
                         [(20000, "Nancy Fard", "2023-11-30"), (50000, "Nadar Khosravi", "2023-12-01")])   # cumulative 700.00 not taken
        self.assertEqual(r["subtotal"], 70000)

    def test_cell_reread_fixes_garbled_amount(self):
        ws = sched_a_page([("12/11/2023", "Sam Goldfeder", "500M0", "500.00"), ("12/11/2023", "Sabrina Goldfeder", "500.00", "500.00")], "1,000.00")
        r = T.rebuild_schedule([(2, ws)], "A", lambda p: (lambda boxes: ["500.00" for _ in boxes]))
        self.assertEqual(r["sum"], 100000)
        self.assertTrue(not r["unread"] and not r["subtotal_mismatch"])

    def test_unreadable_cell_stays_unread(self):
        ws = sched_a_page([("12/11/2023", "Sam Goldfeder", "500M0", "500.00")])
        r = T.rebuild_schedule([(2, ws)], "A", lambda p: (lambda boxes: ["" for _ in boxes]))
        self.assertEqual(r["unread"], 1)

    def test_subtotal_mismatch_reported(self):
        ws = sched_a_page([("11/30/2023", "Nancy Fard", "200.00", "200.00")], "300.00")
        r = T.rebuild_schedule([(2, ws)], "A")
        self.assertEqual(r["subtotal_mismatch"], [2])

    def test_match_rows(self):
        ex = [{"id": "a", "page": 2, "amount": 20000, "ord": 1}, {"id": "b", "page": 2, "amount": None, "ord": 2}, {"id": "c", "page": 3, "amount": 5, "ord": 3}]
        rb = [{"page": 2, "amount": 20000, "y": 1}, {"page": 2, "amount": 50000, "y": 2}, {"page": 4, "amount": 100, "y": 1}]
        pairs, ex_left, rb_left = T.match_rows(ex, rb)
        self.assertEqual([(e["id"], r["amount"]) for e, r in pairs], [("a", 20000), ("b", 50000)])
        self.assertEqual([e["id"] for e in ex_left], ["c"])
        self.assertEqual([r["amount"] for r in rb_left], [10000 // 100])


class TestRobotInReview(unittest.TestCase):
    def pages(self, words):
        a = {1: "Campaign Disclosure Statement Summary Page", 2: BOX_A, 3: "Schedule E Payments Made"}
        return FakePages(a, words={2: words})

    def test_rebuild_fills_misread_rows(self):
        ws = sched_a_page([("02/01/2018", "Jane Doe", "10,000.00", "10,000.00"), ("02/02/2018", "John Roe", "6,930.00", "6,930.00")], "16,930.00")
        rows = [row(1, "460A", 1000000), row(2, "460A", None, name="l", date="2020-01-01")]
        r = R.review_filing(filing(), rows, self.pages(ws), deadline=9e18)
        self.assertEqual(r["fail"], [])
        got = {(c["transaction_id"], c["field"]): c["after"] for c in r["corrections"]}
        self.assertEqual(got[("ct_2", "amount_minor")], 693000)
        self.assertEqual(got[("ct_2", "counterparty_name_raw")], "John Roe")
        self.assertEqual(got[("ct_2", "transaction_date")], "2018-02-02")
        self.assertEqual(r["checks"]["A"]["fixed"], "rebuilt_rows")

    def test_rebuild_inserts_missed_row_and_rejects_unbacked(self):
        ws = sched_a_page([("02/01/2018", "Jane Doe", "10,000.00", "10,000.00"), ("02/02/2018", "John Roe", "6,930.00", "6,930.00")], "16,930.00")
        rows = [row(1, "460A", 1000000), dict(row(3, "460A", 1200, page=3, name="OF BUSINESS)"))]
        r = R.review_filing(filing(), rows, self.pages(ws), deadline=9e18)
        self.assertEqual(r["fail"], [])
        rules = sorted(c["rule"] for c in r["corrections"])
        self.assertEqual(rules, ["insert_rebuilt_row", "reject_unbacked_row"])
        eff = R.effective_rows(rows, r, filing())
        self.assertEqual(sum(t["amount_minor"] for t in eff), 1693000)
        ins = next(c for c in r["corrections"] if c["rule"] == "insert_rebuilt_row")
        sql = R.correction_sql(ins, "cf_1", rows, filing(), "run-x", "2026-10-17T00:00:00Z")[0]
        self.assertIn("INSERT OR IGNORE INTO campaign_transactions", sql)
        self.assertIn("'clerk:" + ins["transaction_id"] + "'", sql)
        self.assertIn("'OCR_UNREVIEWED'", sql)

    def test_rebuild_not_used_when_it_does_not_reconcile(self):
        ws = sched_a_page([("02/01/2018", "Jane Doe", "10,000.00", "10,000.00"), ("02/02/2018", "John Roe", "6,000.00", "6,000.00")], "16,000.00")
        rows = [row(1, "460A", 1000000)]
        r = R.review_filing(filing(), rows, self.pages(ws), deadline=9e18)
        self.assertIn("A:rows_do_not_reconcile", r["fail"])
        self.assertEqual(r["corrections"], [])

    def test_rebuilt_rows_settle_a_disagreeing_box(self):
        ws = sched_a_page([("02/01/2018", "Jane Doe", "10,000.00", "10,000.00"), ("02/02/2018", "John Roe", "6,930.00", "6,930.00")], "16,930.00")
        p = self.pages(ws)
        p.t["B"] = dict(p.t["A"]); p.t["B"][2] = BOX_A.replace("16,930.00", "18,930.00").replace("17,035.00", "19,035.00")
        p.t["C"] = dict(p.t["A"]); p.t["C"][2] = BOX_A.replace("16,930.00", "16,980.00").replace("17,035.00", "17,085.00")
        r = R.review_filing(filing(), [row(1, "460A", 1000000), row(2, "460A", 693000)], p, deadline=9e18)
        self.assertEqual(r["fail"], [])
        self.assertEqual(r["checks"]["A"]["settled_by"], "rebuilt_rows_match_one_read")


if __name__ == "__main__":
    unittest.main()
