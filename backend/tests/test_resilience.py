"""The report run and a lost connection: it waits for the server instead of failing, checks
again what broke off, keeps what was measured, and a finished run can be analysed again.
No database is used.

Run from backend/:  python -m pytest tests -q
"""
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from app.report import collect, jobs, settings, synth

UP = {"ok": True, "label": "ATNM", "error": None}
DOWN = {"ok": False, "label": "ATNM", "error": "cannot be reached"}
RDS = {"ok": True, "label": "RDS", "error": None}


def pf(atnm):
    return {"ok": atnm["ok"], "atnm": atnm, "rds": RDS, "busy": [], "plan": {"ok": True}, "ai": {"enabled": False}}


class LostConnection(unittest.TestCase):
    def setUp(self):
        jobs._cancel.clear()
        for p in (mock.patch.object(jobs, "RETRY_WAIT", 0), mock.patch.object(jobs, "_stage")):
            p.start()
            self.addCleanup(p.stop)

    def test_only_a_lost_connection_counts(self):
        self.assertTrue(jobs._lost_connection({"status": "error", "headline": "Check failed: ('08S01', 'TCP Provider')"}))
        self.assertFalse(jobs._lost_connection({"status": "error", "headline": "Check failed: invalid column"}))
        self.assertFalse(jobs._lost_connection({"status": "problems", "headline": "08S01 in a value"}))
        self.assertFalse(jobs._lost_connection(None))

    def test_waits_until_the_server_answers_again(self):
        answers = iter([pf(DOWN), pf(DOWN), pf(UP)])
        with mock.patch.object(jobs, "preflight", side_effect=lambda: next(answers)):
            self.assertTrue(jobs._wait_for_servers("collect", "The final count check"))
        texts = [x["text"] for x in jobs.log.all()]
        self.assertTrue(any("cannot be reached" in t for t in texts))
        self.assertTrue(any("answers again" in t for t in texts))

    def test_stop_ends_the_wait(self):
        def down():
            jobs._cancel.set()
            return pf(DOWN)
        with mock.patch.object(jobs, "preflight", side_effect=down):
            self.assertFalse(jobs._wait_for_servers("collect", "The final count check"))
        jobs._cancel.clear()

    def test_a_mapping_that_broke_off_is_checked_again(self):
        m1, m2 = mock.Mock(id="m1"), mock.Mock(id="m2")
        fake_main = mock.MagicMock()
        fake_main.get_plan.return_value.mappings = [m1, m2]
        lost = {"m1": [True, False]}          # m1 broke off once, then is fine

        def is_lost(mid, since):
            seq = lost.get(mid)
            return seq.pop(0) if seq else False

        follow = mock.Mock(return_value=True)
        with mock.patch.dict("sys.modules", {"app.main": fake_main}), \
                mock.patch("app.main", fake_main, create=True), \
                mock.patch.object(jobs, "_mapping_lost", side_effect=is_lost), \
                mock.patch.object(jobs, "_wait_for_servers", return_value=True), \
                mock.patch.object(jobs, "_follow_part2", follow), \
                mock.patch.object(jobs, "job", {**jobs._blank_job(), "test": None}):
            jobs._part2(False, "2026-10-03T00:00:00+00:00")
        self.assertEqual(follow.call_count, 2)
        self.assertEqual([m.id for m in follow.call_args_list[1][0][1]], ["m1"])      # only the one that broke off

    def test_retries_stop_after_the_set_rounds(self):
        m1 = mock.Mock(id="m1")
        fake_main = mock.MagicMock()
        fake_main.get_plan.return_value.mappings = [m1]
        follow = mock.Mock(return_value=True)
        with mock.patch.dict("sys.modules", {"app.main": fake_main}), \
                mock.patch("app.main", fake_main, create=True), \
                mock.patch.object(jobs, "_mapping_lost", return_value=True), \
                mock.patch.object(jobs, "_wait_for_servers", return_value=True), \
                mock.patch.object(jobs, "_follow_part2", follow), \
                mock.patch.object(jobs, "job", {**jobs._blank_job(), "test": None}):
            jobs._part2(False, "2026-10-03T00:00:00+00:00")
        self.assertEqual(follow.call_count, 1 + jobs.RETRY_ROUNDS)


class KeepWhatWasMeasured(unittest.TestCase):
    def test_the_catalog_read_during_the_run_replaces_an_unreachable_one(self):
        from app.ATNM import catalog
        from app.ATNM import settings as atnm_settings
        pair = mock.Mock(id="1", source_db="AppDb", target_db="AppDb_Copy")
        since = "2026-10-03T10:00:00+00:00"
        t0 = datetime.fromisoformat(since).timestamp()
        during, before = {"tables": {}, "read_at": t0 + 60}, {"tables": {}, "read_at": t0 - 60}
        cats = {("1", "source"): {"error": {"message": "cannot be reached"}}, ("1", "target"): {"tables": {}}}
        with mock.patch.dict(catalog._cache, {(atnm_settings.SOURCE.key, "appdb"): during}, clear=True):
            replaced = collect._fall_back_to_run_catalogs([pair], cats, since)
        self.assertIs(cats[("1", "source")], during)
        self.assertEqual(replaced, {"1": [("source", t0 + 60)]})
        # a catalog read before the run is never used
        cats = {("1", "source"): {"error": {"message": "x"}}, ("1", "target"): {"tables": {}}}
        with mock.patch.dict(catalog._cache, {(atnm_settings.SOURCE.key, "appdb"): before}, clear=True):
            self.assertEqual(collect._fall_back_to_run_catalogs([pair], cats, since), {})
        self.assertIn("error", cats[("1", "source")])


class AnalyseAgain(unittest.TestCase):
    def test_a_finished_run_is_analysed_again_without_checking_tables(self):
        from test_report import evidence
        with tempfile.TemporaryDirectory() as d:
            ev = evidence(("Correct",), ("Correct",))
            ev["run"].update(id="R1", mode="Full live check", expected_tables=1, expected_mappings=1)
            (Path(d) / "R1").mkdir()
            (Path(d) / "R1" / "evidence.json").write_text(json.dumps(ev), encoding="utf-8")
            seen = {}

            def build(run, since):
                seen.update(run=run, since=since)
                return {**ev, "run": {**ev["run"], **run}}

            with mock.patch.object(settings, "REPORT_DIR", Path(d)), \
                    mock.patch.object(settings, "RUN_FILE", Path(d) / "run.json"), \
                    mock.patch.object(settings, "TEST_TABLES", 0), \
                    mock.patch.object(jobs, "preflight", return_value=pf(UP)), \
                    mock.patch.object(jobs, "_part1") as part1, mock.patch.object(jobs, "_part2") as part2, \
                    mock.patch("app.report.collect.build", side_effect=build), \
                    mock.patch.object(synth, "write", side_effect=lambda e, note=None: {"source": "template", **synth.template(e)}):
                ok, msg = jobs.reanalyse("R1")
                self.assertTrue(ok, msg)
                for _ in range(100):
                    if not jobs.job["running"]:
                        break
                    time.sleep(0.1)
                st = jobs.status(0)
            self.assertIsNone(st["error"], st["error"])
            part1.assert_not_called()
            part2.assert_not_called()                                 # no table checked again
            self.assertEqual(seen["since"], ev["run"]["started_at"])  # the run's own results
            self.assertTrue(seen["run"]["mode"].endswith(jobs.AGAIN))
            self.assertEqual(st["result"]["self_check"], [])
            self.assertEqual([s["status"] for s in st["stages"]], ["done", "skipped", "skipped", "done", "done", "done"])
            # again and again: the mode says it once
            self.assertEqual(jobs.reanalyse("missing")[0], False)

    def test_refused_while_a_report_is_being_made(self):
        with mock.patch.object(jobs, "job", {**jobs._blank_job(), "running": True}):
            self.assertEqual(jobs.reanalyse("R1"), (False, "A report is being made."))


if __name__ == "__main__":
    unittest.main()
