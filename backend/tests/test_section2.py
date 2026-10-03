"""Section 2 (RDS -> new system): a name pair to an empty column, lists found by a column's
name, and one report line per column. No database is used.

Run from backend/:  python -m pytest tests -q
"""
import unittest
from unittest import mock

from app import compare
from app import datacheck as dc
from app.report import collect, plain


def col(name, type_="int", pk=False):
    return {"name": name, "type": type_, "nullable": True, "pk": pk, "identity": False}


class EmptyNamePair(unittest.TestCase):
    """ID -> id by name, while id is empty and the ids are in equipment_type_id."""

    SRC = [col("ID", "nvarchar(255)"), col("Code", "nvarchar(255)")]
    TGT = [col("equipment_type_id", pk=True), col("id"), col("code", "nvarchar(50)")]

    def by_source(self, rows):
        return {r["source"]["name"]: r for r in rows if r["source"]}

    def test_without_a_profile_the_name_pair_stands(self):
        rows, _ = compare.align_columns(self.SRC, self.TGT, target_table="equipment_type")
        self.assertEqual(self.by_source(rows)["ID"]["target"]["name"], "id")

    def test_an_empty_target_column_is_released(self):
        rows, summary = compare.align_columns(self.SRC, self.TGT, target_table="equipment_type",
                                              empty_targets={"id"}, filled_sources={"id", "code"})
        r = self.by_source(rows)["ID"]
        self.assertIsNone(r["target"])
        self.assertEqual(r["status"], "source_only")
        self.assertEqual(r["released"], "id")
        self.assertIn("id", [x["target"]["name"] for x in rows if x["status"] == "target_only"])
        self.assertEqual(self.by_source(rows)["Code"]["target"]["name"], "code")   # others untouched
        self.assertEqual(summary["released"], 1)

    def test_not_released_when_the_source_is_empty_too(self):
        rows, _ = compare.align_columns(self.SRC, self.TGT, target_table="equipment_type",
                                        empty_targets={"id"}, filled_sources={"code"})
        self.assertEqual(self.by_source(rows)["ID"]["target"]["name"], "id")

    def test_a_declared_pair_is_never_released(self):
        rows, _ = compare.align_columns(self.SRC, self.TGT, target_table="equipment_type", declared={"ID": "id"},
                                        empty_targets={"id"}, filled_sources={"id"})
        self.assertEqual(self.by_source(rows)["ID"]["target"]["name"], "id")

    def test_emptiness_from_a_saved_profile(self):
        result = {"target": "ref.equipment_type", "profile": {
            "source_rows": 91, "target_rows": 91,
            "source": [{"column": "ID", "nulls": 0, "blanks": 0}, {"column": "Date", "nulls": 91, "blanks": 0}],
            "target": [{"column": "id", "nulls": 91, "blanks": 0}, {"column": "code", "nulls": 0, "blanks": 0},
                       {"column": "name", "nulls": 50, "blanks": 41}]}}
        empty, filled = dc.emptiness(result, "REF.equipment_type")
        self.assertEqual(empty, {"id", "name"})        # NULL or blank on every row
        self.assertEqual(filled, {"id"})
        self.assertEqual(dc.emptiness(result, "ref.other"), (None, None))   # measured on another table
        self.assertEqual(dc.emptiness(None), (None, None))


class LookupByColumnName(unittest.TestCase):
    """cluster_code has no foreign key: the list is found by the column's name."""

    def run_infer(self, coverages):
        tables = [{"oid": n, "sch": "ref", "tbl": f"list{n}", "col": "cluster_code"} for n in range(len(coverages))]

        def find(side, pair, fk):
            cov = coverages[fk["referenced_object_id"]]
            return None if cov is None else {"schema": "ref", "table": fk["referenced"]["table"], "column": "name",
                                             "type": "nvarchar(50)", "key": "cluster_code", "coverage": cov,
                                             "distinct_values": 20, "distinct_found": round(cov * 20)}

        pair = mock.Mock(target={"name": "cluster_code"})
        with mock.patch.object(dc.db, "query", return_value=tables), mock.patch.object(dc, "_find_lookup", side_effect=find):
            return dc._infer_lookup(None, pair, {"object_id": 99})

    def test_the_one_list_that_holds_the_values_is_used(self):
        found, options = self.run_infer([None, 1.0])          # an empty list, and the real one
        self.assertEqual(found["table"], "list1")
        self.assertTrue(found["inferred"])
        self.assertEqual(options, [])

    def test_two_lists_that_both_hold_every_value_are_not_guessed(self):
        found, options = self.run_infer([1.0, 1.0])
        self.assertIsNone(found)
        self.assertEqual(options, ["ref.list0", "ref.list1"])

    def test_only_a_list_holding_every_value_is_used(self):
        found, _ = self.run_infer([1.0, 0.95])          # 19 of 20 is not enough
        self.assertEqual(found["table"], "list0")
        found, options = self.run_infer([0.95])
        self.assertIsNone(found)
        self.assertEqual(options, ["ref.list0"])

    def test_findings_say_how_the_list_was_found(self):
        c = {"label": "Cluster → cluster_code", "mode": "lookup", "buckets": dict.fromkeys(dc.BUCKETS, 0),
             "lookup": {"schema": "ref", "table": "cluster", "column": "cluster_name", "inferred": True}}
        verdict, fs = dc._column_verdict(c)
        self.assertEqual(verdict, "identical")
        self.assertIn("no foreign key is declared", fs[0][1])


class ExtraFeeder(unittest.TestCase):
    """Type_Code feeds project_type_id; Type (the list's name) is checked as one more link on it."""

    def make(self, seen, paired=()):
        from app import keymap
        k, name, code = col("project_type_id"), col("project_type", "nvarchar(100)"), col("project_type_code", "varchar(10)")
        fk = {"name": "fk", "referenced": {"schema": "project", "table": "project_type"}}
        lk = keymap.Link(0, fk, col("project_type_id"), k, [k, name, code])
        lk.s, lk.how, lk.r, lk.mode = col("Type_Code", "nvarchar(50)"), "found", code, "lookup"
        lk.seen = seen(name, code)
        prep = mock.Mock(rows=[{"source": col(n), "target": col(n.lower())} for n in paired])
        links = [lk]
        keymap._extra_feeders(prep, links)
        return links

    def test_a_name_column_of_the_same_list_is_checked_too(self):
        links = self.make(lambda name, code: {
            "type_code": (col("Type_Code", "nvarchar(50)"), code, 10, 10),
            "type": (col("Type", "nvarchar(50)"), name, 10, 10)})
        self.assertEqual(len(links), 2)
        x = links[1]
        self.assertEqual((x.s["name"], x.r["name"], x.extra_of, x.mode, x.j), ("Type", "project_type", "Type_Code", "lookup", 1))

    def test_not_for_paired_columns_or_too_few_values(self):
        links = self.make(lambda name, code: {
            "type": (col("Type", "nvarchar(50)"), name, 10, 10),
            "site": (col("Site", "nvarchar(50)"), name, 9, 10)}, paired=["Type"])     # 9 of 10 is not all
        self.assertEqual(len(links), 1)


class OneLinePerColumn(unittest.TestCase):
    def test_findings_about_one_column_are_one_line(self):
        D, M = plain.DECIDE, plain.MUST_FIX
        issues = [
            ("Renamed column", D, "Cluster and cluster_code are paired by name, but the data does not agree.",
             "Cluster → cluster_code"),
            ("Column type", D, "Cluster: the data type changed and the values differ.", "Cluster"),
            ("Values", M, "Cluster → cluster_code: 2 values lost.", "Cluster → cluster_code"),
            ("Values", D, "All 5 rows are identical apart from the column to review: Cluster → cluster_code.", ""),
            ("Column not migrated", D, "1 column of the source table is not in the target table: CreatedOn.", "CreatedOn"),
        ]
        out = collect._merge_by_column(issues)
        self.assertEqual(len(out), 2)                    # Cluster (3 findings) + CreatedOn; the row line dropped
        kind, result, text, column = out[0]
        self.assertEqual((result, column), (M, "Cluster → cluster_code"))      # the worst part decides
        self.assertTrue(text.startswith("Cluster → cluster_code: "))
        self.assertIn("2 values lost", text)
        self.assertIn("data type changed", text)
        self.assertEqual(text.count("Cluster → cluster_code:"), 1)
        self.assertEqual(out[1][3], "CreatedOn")

    def test_a_row_line_naming_an_unexplained_column_stays(self):
        D = plain.DECIDE
        issues = [("Values", D, "Rows differ only in the columns to review: A → a, B → b.", ""),
                  ("Values", D, "A → a: recoded.", "A → a")]
        self.assertEqual(len(collect._merge_by_column(issues)), 2)


if __name__ == "__main__":
    unittest.main()
