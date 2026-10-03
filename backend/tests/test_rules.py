"""Tests of the decision rules, on fixed sample data: no database is touched.

Run from backend/:  python -m pytest tests -q   (or: python -m unittest discover tests)
"""
import threading
import time
import unittest
from types import SimpleNamespace

from app import datacheck, identity, runguard
from app.ATNM import datacheck as atnm_dc
from app.ATNM import settings as atnm_settings
from app.ATNM import structure


class IdentityCounter(unittest.TestCase):
    def row(self, seed=1, inc=1, last=None, hi=None, lo=None):
        return {"seed": seed, "inc": inc, "last_value": last, "hi": hi, "lo": lo}

    def test_in_step(self):
        s = identity.state("id", self.row(last=100, hi=100, lo=1))
        self.assertEqual(s["next_value"], 101)
        self.assertFalse(s["behind"])

    def test_behind(self):
        s = identity.state("id", self.row(last=50, hi=100, lo=1))
        self.assertTrue(s["behind"])
        self.assertIn("will fail", identity.text("dbo.t", s))

    def test_never_used_counter_with_copied_rows(self):
        self.assertTrue(identity.state("id", self.row(seed=1, last=None, hi=10, lo=1))["behind"])

    def test_empty_table(self):
        self.assertFalse(identity.state("id", self.row(last=None, hi=None, lo=None))["behind"])

    def test_counting_down(self):
        self.assertTrue(identity.state("id", self.row(seed=-1, inc=-1, last=-5, hi=-1, lo=-9))["behind"])
        self.assertFalse(identity.state("id", self.row(seed=-1, inc=-1, last=-9, hi=-1, lo=-9))["behind"])


class KeysAndRules(unittest.TestCase):
    SRC = {"primary_key": ["Id"], "unique": [["Code"]],
           "foreign_keys": [{"columns": ["UomId"], "ref_table": "dbo.Uom", "ref_columns": ["Id"],
                             "enabled": True, "trusted": True}],
           "defaults": {"CreatedOn": "(getdate())", "Active": "((1))"},
           "checks": [{"definition": "([Qty]>=(0))", "enabled": True}]}

    def kinds(self, rows):
        return {(r["kind"], r["status"]): r["severity"] for r in rows}

    def test_identical_copy(self):
        rows = structure.compare_constraints(self.SRC, self.SRC)
        self.assertTrue(all(r["status"] == "same" for r in rows))
        self.assertEqual(structure.constraint_summary(rows)["severity"], "ok")

    def test_lost_and_changed(self):
        tgt = {"primary_key": None, "unique": [], "foreign_keys": [
            {"columns": ["uomid"], "ref_table": "dbo.uom", "ref_columns": ["id"], "enabled": True, "trusted": False}],
               "defaults": {"createdon": "( getdate() )", "Active": "((0))"}, "checks": []}
        k = self.kinds(structure.compare_constraints(self.SRC, tgt))
        self.assertEqual(k[("primary key", "missing")], "problem")
        self.assertEqual(k[("unique key", "missing")], "review")
        self.assertEqual(k[("foreign key", "changed")], "review")      # not trusted in RDS
        self.assertEqual(k[("default value", "same")], "ok")           # same rule, other spacing and case
        self.assertEqual(k[("default value", "changed")], "review")
        self.assertEqual(k[("check rule", "missing")], "review")

    def test_primary_key_on_other_columns(self):
        tgt = dict(self.SRC, primary_key=["Code"])
        k = self.kinds(structure.compare_constraints(self.SRC, tgt))
        self.assertEqual(k[("primary key", "changed")], "problem")

    def test_only_in_rds_is_listed_not_graded(self):
        src = dict(self.SRC, primary_key=None)
        k = self.kinds(structure.compare_constraints(src, self.SRC))
        self.assertEqual(k[("primary key", "extra")], "ok")

    def test_unreadable(self):
        self.assertIsNone(structure.compare_constraints(None, self.SRC))
        self.assertIsNone(structure.constraint_summary(None))


def col(name, type_="nvarchar(50)", base="nvarchar", nullable=True):
    return {"name": name, "type": type_, "base": base, "nullable": nullable, "identity": False, "computed": False,
            "collation": None, "max_length": 100}


class AtnmRenamesShown(unittest.TestCase):
    def test_verified_rename_replaces_missing_and_extra(self):
        rows = structure.compare_columns([col("Id"), col("Type")], [col("Id"), col("project_type")])
        saved = {"renames": [{"source": "Type", "target": "project_type", "verdict": "verified", "paired": 375}]}
        out = structure.apply_renames(rows, saved)
        self.assertEqual([r["status"] for r in out], ["same", "renamed"])
        self.assertEqual(out[1]["target"]["name"], "project_type")
        summary = structure.column_summary(out)
        self.assertEqual(summary["missing"], [])
        self.assertEqual(summary["extra"], [])

    def test_possible_rename_changes_nothing(self):
        rows = structure.compare_columns([col("Type")], [col("project_type")])
        saved = {"renames": [{"source": "Type", "target": "project_type", "verdict": "possible", "paired": 375}]}
        self.assertEqual([r["status"] for r in structure.apply_renames(rows, saved)], ["missing", "extra"])

    def test_old_results_without_renames(self):
        rows = structure.compare_columns([col("Type")], [col("project_type")])
        self.assertEqual(structure.apply_renames(rows, {"status": "identical"}), rows)


class FakeRun:
    """Stands in for the two server connections: returns fixed rows."""

    def __init__(self, rs, rt):
        self.rs, self.rt = rs, rt
        self.ctx = SimpleNamespace(note=lambda *a, **k: None)

    def both(self, make_sql, params=None, what=None):
        return self.rs, self.rt


def side(rows, keyed=True):
    return SimpleNamespace(entry={"rows": rows}, key_idx=[0] if keyed else [])


class AtnmRenameDecision(unittest.TestCase):
    """_find_renames on rows of (pair key, value hash, filled flag) per candidate column."""

    def rows(self, values):
        return [{"p": k, "v0": v, "f0": 0 if v is None else 1} for k, v in values]

    def decide(self, src_vals, tgt_vals, n_rows=None):
        s, t = side(n_rows or len(src_vals)), side(n_rows or len(tgt_vals))
        run = FakeRun(self.rows(src_vals), [{"p": r["p"], "v0": r["v0"], "f0": r["f0"]} for r in self.rows(tgt_vals)])
        out, _ = atnm_dc._find_renames(run, s, t, [col("Type")], [col("project_type")])
        return out

    def test_identical_on_every_row_is_verified(self):
        vals = [(i, i % 7) for i in range(300)]
        out = self.decide(vals, vals)
        self.assertEqual(out[0]["verdict"], "verified")

    def test_one_differing_row_is_not_verified(self):
        vals = [(i, i % 7) for i in range(300)]
        other = vals[:-1] + [(299, 99)]
        out = self.decide(vals, other)
        self.assertEqual(out[0]["verdict"], "possible")
        self.assertIn("differs on 1 of 300", out[0]["reason"])

    def test_one_value_everywhere_is_not_proof(self):
        vals = [(i, 5) for i in range(300)]
        self.assertNotEqual(self.decide(vals, vals)[0]["verdict"], "verified")

    def test_too_few_rows_paired(self):
        src = [(i, i % 7) for i in range(300)]
        tgt = [(i, i % 7) for i in range(100)] + [(1000 + i, 1) for i in range(200)]
        out = self.decide(src, tgt)
        self.assertNotEqual(out[0]["verdict"], "verified")

    def test_over_the_row_limit_is_not_read(self):
        big = atnm_settings.DIFF_ROWS_MAX + 1
        out, note = atnm_dc._find_renames(FakeRun([], []), side(big), side(big), [col("Type")], [col("project_type")])
        self.assertEqual(out, [])
        self.assertIn("ATNM_DIFF_ROWS_MAX", note)


class LargeColumns(unittest.TestCase):
    def pair(self, s, t):
        return {"source": {"type": s}, "target": {"type": t}}

    def test_kinds(self):
        self.assertEqual(datacheck._large_kind(self.pair("text", "text")), "text")
        self.assertEqual(datacheck._large_kind(self.pair("ntext", "nvarchar(max)")), "text")
        self.assertEqual(datacheck._large_kind(self.pair("xml", "xml")), "text")
        self.assertEqual(datacheck._large_kind(self.pair("image", "varbinary(max)")), "binary")
        self.assertIsNone(datacheck._large_kind(self.pair("text", "image")))          # different kinds
        self.assertIsNone(datacheck._large_kind(self.pair("geography", "geography")))  # not a large text/binary
        self.assertIsNone(datacheck._large_kind(self.pair("int", "int")))              # compared normally


class RunGuard(unittest.TestCase):
    def setUp(self):
        # Independent of RUN_PAUSE_SECONDS in backend/.env.
        self.pause = runguard.PAUSE
        runguard.PAUSE = 0
        runguard._state["free_at"] = 0.0

    def tearDown(self):
        runguard.PAUSE = self.pause
        runguard._state["free_at"] = 0.0

    def test_pause_leaves_a_gap_between_steps(self):
        runguard.PAUSE = 0.4
        with runguard.slot("first"):
            pass
        started = time.time()
        with runguard.slot("second"):
            waited = time.time() - started
        self.assertGreaterEqual(waited, 0.35)

    def test_reentrant_in_one_thread(self):
        with runguard.slot("outer"):
            with runguard.slot("inner"):
                self.assertEqual(runguard.holder(), "outer")
        self.assertIsNone(runguard.holder())

    def test_two_runs_take_turns(self):
        order = []

        def work(name):
            with runguard.slot(name):
                order.append(f"{name} in")
                time.sleep(0.2)
                order.append(f"{name} out")

        a = threading.Thread(target=work, args=("a",))
        b = threading.Thread(target=work, args=("b",))
        a.start()
        time.sleep(0.05)
        b.start()
        a.join()
        b.join()
        self.assertEqual(order, ["a in", "a out", "b in", "b out"])

    def test_cancel_while_waiting(self):
        held, release = threading.Event(), threading.Event()

        def holder():
            with runguard.slot("long step"):
                held.set()
                release.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        held.wait(5)
        waited = []
        with self.assertRaises(runguard.Cancelled):
            with runguard.slot("second", cancelled=lambda: True, on_wait=waited.append):
                pass
        release.set()
        t.join()


if __name__ == "__main__":
    unittest.main()
