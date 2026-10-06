"""The migration plan's copy_only list: tables checked as an ATNM -> RDS copy only.
No database is used.

Run from backend/:  python -m pytest tests -q
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import config, mapping

PLAN = """
copy_only: {copy_only}
mappings:
  - id: company
    type: one_to_one
    sources: [A.dbo.Company]
    targets: [T.ref.company]
"""


def load(copy_only):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "plan.yaml"
        path.write_text(PLAN.format(copy_only=copy_only), encoding="utf-8")
        return mapping.load(path)


class CopyOnly(unittest.TestCase):
    def test_listed_tables_are_kept_and_mappings_unchanged(self):
        plan = load("[A.dbo.Location, A.dbo.employees_V2]")
        self.assertEqual([r.ref for r in plan.copy_only], ["A.dbo.Location", "A.dbo.employees_V2"])
        self.assertEqual([m.id for m in plan.mappings], ["company"])
        self.assertFalse(plan.contains(plan.copy_only[0]))        # not part of any mapping

    def test_no_list_is_fine(self):
        self.assertEqual(load("[]").copy_only, [])

    def test_bad_entries_are_refused(self):
        for bad, why in (("[T.dbo.Location]", "not in a source database"),
                         ("[A.dbo.Company]", "already a source"),
                         ("[A.dbo.Location, a.DBO.location]", "listed twice")):
            with self.assertRaises(mapping.PlanError) as ctx:
                load(bad)
            self.assertIn(why, str(ctx.exception))

    def test_required_tables_include_copy_only_without_a_mapping(self):
        from app.ATNM import required
        plan = load("[A.dbo.Location]")
        pair = mock.Mock(target_db=config.DATABASES["A"]["name"])
        with mock.patch.object(required, "_plan", return_value=plan):
            req = required.tables(pair)
        self.assertEqual(req["dbo.company"]["mapping"], "company")
        self.assertEqual(req["dbo.location"], {"schema": "dbo", "table": "Location", "mapping": None})


NAMED = """
atnm_names: {names}
mappings:
  - id: employees_v2
    type: one_to_one
    sources: [A.dbo.Employees_V2]
    targets: [T.ref.employees_v2]
"""


def load_named(names):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "plan.yaml"
        path.write_text(NAMED.format(names=names), encoding="utf-8")
        return mapping.load(path)


class AtnmNames(unittest.TestCase):
    """Employees_V2 on RDS is the copy of Employee_v2 on ATNM."""

    def test_declared_pairs_are_kept(self):
        plan = load_named("{A.dbo.Employees_V2: dbo.Employee_v2}")
        self.assertEqual(plan.atnm_names, {"a.dbo.employees_v2": "dbo.Employee_v2"})

    def test_bad_entries_are_refused(self):
        for bad, why in (("{T.ref.x: dbo.y}", "not in a source database"),
                         ("{A.dbo.Employees_V2: Employee_v2}", "<schema>.<table>"),
                         ("{A.dbo.a: dbo.x, A.dbo.b: DBO.X}", "already the copy"),
                         ("[A.dbo.a]", "must map")):
            with self.assertRaises(mapping.PlanError) as ctx:
                load_named(bad)
            self.assertIn(why, str(ctx.exception))

    def test_the_atnm_table_is_filed_under_its_rds_name(self):
        from app.ATNM import required
        plan = load_named("{A.dbo.Employees_V2: dbo.Employee_v2}")
        pair = mock.Mock(target_db=config.DATABASES["A"]["name"])
        with mock.patch.object(required, "_plan", return_value=plan):
            names = required.atnm_names(pair)
            self.assertEqual(names, {"dbo.employee_v2": "dbo.employees_v2"})
            self.assertIn("dbo.employees_v2", required.tables(pair))
        cat = {"tables": {"dbo.employee_v2": {"key": "dbo.employee_v2", "schema": "dbo", "table": "Employee_v2"},
                          "dbo.other": {"key": "dbo.other", "schema": "dbo", "table": "Other"}}}
        out = required.as_rds_names(cat, names)
        e = out["tables"]["dbo.employees_v2"]
        self.assertEqual((e["key"], e["schema"], e["table"]), ("dbo.employees_v2", "dbo", "Employee_v2"))  # real name kept
        self.assertNotIn("dbo.employee_v2", out["tables"])
        self.assertIn("dbo.employee_v2", cat["tables"])          # the shared catalog itself is not changed
        self.assertIs(required.as_rds_names({"error": "x"}, names)["error"], "x")


if __name__ == "__main__":
    unittest.main()
