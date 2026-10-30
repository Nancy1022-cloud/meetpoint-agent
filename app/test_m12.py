"""M12 integrity and failure-injection tests. No real model or map requests."""
from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from app import m12
from app.m8 import no_network
from app.m8_cases import suite, digest
from app.m8_trace import append_json
from app.m10_eval import run_case
from app.m12_report import report, result_exit, stability, forecast, package


class M12Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = suite()
        cls.case = deepcopy(cls.data["dev"][0])
        cls.events = []
        with no_network():
            cls.row = run_case(cls.case, "V2", "scripted", "t1", sink=cls.events.append)
        cls.row["repeat"] = 1

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/"app").mkdir()
        (self.root/"app/frozen.py").write_text("# frozen\n", encoding="utf-8")
        self.batch = self.root/"output/smoke"
        self.batch.mkdir(parents=True)
        self.root_patch = patch.object(m12, "ROOT", self.root)
        self.root_patch.start()
        self.m = {"milestone": "M12", "batch_id": "test", "stage": "smoke", "mode": "scripted", "split": "dev",
            "model": "not_called", "primary_policy": "V2", "repeats": 1, "policies": ["V2"],
            "trials": [{"trial_id": "t1", "case_id": self.case["id"], "policy": "V2", "mode": "scripted", "repeat": 1}],
            "selected_cases_sha256": digest([self.case]), "source_files_sha256": m12.source_files(),
            "thresholds": {"acceptance_each_group": .9},
            "known_M11_NL": {"passed": 31, "planned": 36, "quality_gate_passed": False},
            "online_validation": "not_executed"}
        self.m["source_digest"] = digest(self.m["source_files_sha256"])
        self.write_manifest()
        m12.dump(self.batch/"cases.json", [self.case])
        for name in self.m["source_files_sha256"]:
            p = self.batch/"source_snapshot"/name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes((self.root/name).read_bytes())
        append_json(self.batch/"results.jsonl", deepcopy(self.row))
        for e in self.events: append_json(self.batch/"events.jsonl", e)

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def write_manifest(self):
        m12.dump(self.batch/"manifest.json", self.m)
        (self.batch/"manifest.sha256").write_text(digest(self.m)+"\n")

    def replace_row(self, row):
        (self.batch/"results.jsonl").write_text(json.dumps(row, ensure_ascii=False)+"\n", encoding="utf-8")

    def test_frozen_distribution_and_formal_counts(self):
        cases = m12.select_cases(self.data, "final")
        plans = m12.plan_trials(cases, "llm", 3)
        self.assertEqual(len(cases), 200)
        self.assertEqual(len(plans), 1680)
        self.assertEqual(sum(t["policy"] in ("V1", "V2") for t in plans), 1200)

    def test_baselines_only_have_planning_tasks(self):
        by_id = {c["id"]: c for c in self.data["holdout"]}
        plans = m12.plan_trials(self.data["holdout"], "llm", 3)
        self.assertTrue(all(by_id[t["case_id"]]["group"] == "planning" for t in plans if t["policy"] in ("B0", "RULE")))

    def test_dev_and_holdout_ids_are_disjoint(self):
        self.assertFalse({c["id"] for c in self.data["dev"]} & {c["id"] for c in self.data["holdout"]})

    def test_pilot_is_dev_only_and_twenty_tasks(self):
        cases = m12.select_cases(self.data, "pilot")
        self.assertEqual(len(cases), 20)
        self.assertTrue(all(c["id"].startswith("dev-") for c in cases))

    def test_repeat_changes_predeclared_arm_order(self):
        plans = m12.plan_trials([self.case], "llm", 2)
        self.assertEqual([t["policy"] for t in plans[:4]], list(m12.POLICIES))
        self.assertEqual([t["policy"] for t in plans[4:]], list(reversed(m12.POLICIES)))

    def test_bad_holdout_count_is_rejected(self):
        data = deepcopy(self.data); data["holdout"].pop()
        with self.assertRaises(ValueError): m12.select_cases(data, "final")

    def test_clean_report_does_not_claim_real_model_or_final_delivery(self):
        r = report(self.batch)
        self.assertTrue(r["measurement_complete"])
        self.assertTrue(r["hard_checks_passed"])
        self.assertFalse(r["real_model_performance_evidence"])
        self.assertFalse(r["M12_delivery_complete"])
        self.assertFalse(r["whole_product_all_quality_goals_passed"])

    def test_case_tampering_rejected(self):
        changed = deepcopy(self.case); changed["expected_outcomes"] = ["refused"]
        m12.dump(self.batch/"cases.json", [changed])
        with self.assertRaisesRegex(ValueError, "哈希"): m12.load_batch(self.batch)

    def test_manifest_tampering_rejected(self):
        changed = deepcopy(self.m); changed["repeats"] = 3
        m12.dump(self.batch/"manifest.json", changed)
        with self.assertRaisesRegex(ValueError, "哈希"): m12.load_batch(self.batch)

    def test_snapshot_tampering_rejected(self):
        (self.batch/"source_snapshot/app/frozen.py").write_text("# changed")
        with self.assertRaisesRegex(ValueError, "快照"): m12.load_batch(self.batch)

    def test_current_source_drift_rejected(self):
        (self.root/"app/frozen.py").write_text("# changed")
        with self.assertRaisesRegex(ValueError, "当前源码"): m12.load_batch(self.batch, current=True)

    def test_missing_trial_is_failure_and_keeps_planned_denominator(self):
        (self.batch/"results.jsonl").unlink()
        r = report(self.batch)
        g = r["by_policy_group"]["V2:planning"]
        self.assertEqual(g["acceptance"]["denominator"], 1)
        self.assertEqual(g["acceptance"]["numerator"], 0)
        self.assertFalse(r["measurement_complete"])
        self.assertEqual(result_exit(r), 3)

    def test_duplicate_result_is_not_counted_twice(self):
        append_json(self.batch/"results.jsonl", deepcopy(self.row))
        r = report(self.batch)
        self.assertFalse(r["measurement_complete"])
        self.assertEqual(r["by_policy_group"]["V2:planning"]["acceptance"]["numerator"], 0)

    def test_missing_tool_event_blocks_integrity(self):
        events = [e for e in self.events if e.get("event") != "tool_start"]
        (self.batch/"events.jsonl").write_text("".join(json.dumps(e)+"\n" for e in events))
        r = report(self.batch)
        self.assertFalse(r["measurement_complete"])

    def test_repeat_identity_mismatch_blocks_integrity(self):
        row = deepcopy(self.row); row["repeat"] = 2; self.replace_row(row)
        r = report(self.batch)
        self.assertFalse(r["measurement_complete"])
        self.assertTrue(r["m12_protocol_errors"])

    def test_unexpected_real_map_traffic_blocks_integrity(self):
        row = deepcopy(self.row); row["telemetry"]["real_map_http"] = 1; self.replace_row(row)
        r = report(self.batch)
        self.assertFalse(r["hard_checks_passed"])

    def test_missing_usage_is_unknown_and_cannot_resume(self):
        row = deepcopy(self.row)
        row["telemetry"]["llm"] = [{"attempt_id": "unknown", "status": "returned", "usage": None, "returned_model": "not_called"}]
        self.replace_row(row)
        r = report(self.batch)
        self.assertIsNone(r["by_policy_group"]["V2:planning"]["cost"]["token_mean"]["total_tokens"])
        with self.assertRaises(ValueError): m12.resumable(self.m, [row], self.events, [])

    def test_resume_only_returns_unstarted_trials(self):
        m = deepcopy(self.m)
        m["trials"].append({**m["trials"][0], "trial_id": "t2", "repeat": 2})
        self.assertEqual([t["trial_id"] for t in m12.resumable(m, [self.row], self.events, [])], ["t2"])

    def test_resume_rejects_started_missing_result(self):
        with self.assertRaises(ValueError): m12.resumable(self.m, [], self.events, [])

    def test_resume_rejects_duplicate_results(self):
        with self.assertRaises(ValueError): m12.resumable(self.m, [self.row, self.row], self.events, [])

    def test_resume_rejects_malformed_json(self):
        with self.assertRaises(ValueError): m12.resumable(self.m, [self.row], self.events, [{"error": "invalid_json"}])

    def test_forged_display_does_not_pass(self):
        row = deepcopy(self.row); row["presentation"]["evidence_valid"] = False; self.replace_row(row)
        r = report(self.batch)
        self.assertFalse(r["hard_checks_passed"])

    def test_online_manual_record_survives_report(self):
        p = self.batch/"ONLINE_VALIDATION.md"; p.write_text("manual result", encoding="utf-8")
        report(self.batch)
        self.assertEqual(p.read_text(), "manual result")

    def test_resume_audits_old_event_records(self):
        m = {**self.m, "_resume_cases": [self.case]}
        events = [e for e in self.events if e.get("event") != "tool_end"]
        with self.assertRaises(ValueError): m12.resumable(m, [self.row], events, [])

    def test_forecast_prices_requests_at_their_tier_not_aggregate_tier(self):
        m = {**self.m, "stage": "pilot", "mode": "llm", "policies": list(m12.POLICIES), "data_root": str(self.root/"data")}
        groups = {}
        for p in m12.POLICIES:
            for group in m12.GROUPS:
                if p in ("B0", "RULE") and group != "planning": continue
                means = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100} if p in ("V1", "V2") else {
                    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                groups[p+":"+group] = {"planned_trials": 1, "unique_case_ids": 1, "cost": {"token_mean": means}}
        summary = {"measurement_complete": True, "hard_checks_passed": True, "by_policy_group": groups}
        with patch("app.m12_report.report", return_value=summary), patch("app.m12_report.load_batch", return_value=(m, [], [], [], [])):
            value = forecast(self.batch)
        self.assertEqual(value["estimated_tokens"]["prompt_tokens"], 1200000)
        self.assertAlmostEqual(value["linear_historical_cost_yuan"], .336)

    def test_package_excludes_env_and_keeps_partial_status(self):
        self.m.update(stage="final", data_root=str(self.root/"data")); self.write_manifest()
        (self.root/".env").write_text("DO_NOT_EXPORT=secret")
        with patch("app.m12_report.ROOT", self.root): value = package(self.batch)
        import zipfile
        with zipfile.ZipFile(value["archive"]) as z:
            self.assertFalse(any(Path(n).name == ".env" for n in z.namelist()))
            self.assertIn("archive_sha256.json", z.namelist())
        self.assertFalse(value["M12_delivery_complete"])

    def test_final_prepare_is_immutable_and_does_not_execute_tasks(self):
        data_root = self.root/"dataset"
        m12.dump(data_root/"suite.json", self.data)
        m12.dump(data_root/"dataset_manifest.json", {"suite_sha256": digest(self.data), "counts": {"dev": 50, "holdout": 200}})
        args = Namespace(stage="final", data_root=str(data_root), root=str(self.root/"prepared"), repeats=3,
            policies="B0,RULE,V1,V2", batch_yuan=2., total_yuan=5.)
        settings = SimpleNamespace(llm_model="qwen3.7-flash", llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
        with patch("app.config.get_settings", return_value=settings), patch("app.m12_report.forecast", return_value={"policies": list(m12.POLICIES), "three_times_buffer_yuan": .5}), redirect_stdout(StringIO()):
            self.assertEqual(m12.prepare(args), 0)
            batch = Path(args.root)/"final"
            self.assertEqual(len(m12.read(batch/"manifest.json")["trials"]), 1680)
            self.assertFalse((batch/"results.jsonl").exists())
            self.assertFalse((batch/"events.jsonl").exists())
            self.assertEqual(m12.prepare(args), 0)
            args.repeats = 2
            with self.assertRaises(ValueError): m12.prepare(args)

    def test_network_guard_rejects_connect(self):
        with no_network(), socket.socket() as sock:
            with self.assertRaisesRegex(RuntimeError, "禁止网络"): sock.connect(("127.0.0.1", 9))


if __name__ == "__main__": unittest.main()
