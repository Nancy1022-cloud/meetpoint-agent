import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.m1_models import BudgetLimits, CreateTask, Requirements, Reservation, StartRun, UpdateTask
from app.m1_store import StateError, StateStore
from app.m3_baseline import finish_planning
from app.m4_synthetic import ScriptedDecider, SyntheticTools, demo_requirements
from app.m5_api import router
from app.m5_checkpoint import MemoryPlanner, checkpoint_path, inspect_checkpoint, saver_for, thread_id
from app.m5_models import MemoryMessage, MemoryPatch, NewMemoryTask
from app.m5_parse import parse_message
from app.m5_store import MemoryStore


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.store = MemoryStore(self.path)
        self.memory = self.create()
        self.sid = self.memory["task"]["session_id"]

    def create(self, user="demo_user", **extra):
        return self.store.create_memory_task({"user_id": user,
            "person_a": {"origin_text": "合成起点A"}, "person_b": {"origin_text": "合成起点B"}, **extra})

    def message(self, text, version=1, **extra):
        return self.store.apply_message(self.sid, {"user_id": "demo_user",
            "expected_version": version, "text": text, **extra})

    def save(self, revision=0, **values):
        return self.store.save_preferences({"user_id": "demo_user", "expected_revision": revision, "values": values})

    def task(self):
        return self.store.get_task("demo_user", self.sid)


class ParseTests(unittest.TestCase):
    def test_explicit_a_duration(self):
        self.assertEqual(parse_message("我这次可以接受60分钟，其他不变")["changes"], {"person_a": {"max_duration_seconds": 3600}})

    def test_b_retained_in_same_sentence(self):
        self.assertEqual(parse_message("我这次可以接受60分钟，朋友仍然最多45分钟，其他不变")["changes"],
                         {"person_a": {"max_duration_seconds": 3600}, "person_b": {"max_duration_seconds": 2700}})

    def test_explicit_both_roles(self):
        self.assertEqual(parse_message("双方最多换乘0次")["changes"],
                         {"person_a": {"max_transfers": 0}, "person_b": {"max_transfers": 0}})

    def test_units(self):
        self.assertEqual(parse_message("我最多1小时，朋友最多步行1公里")["changes"],
                         {"person_a": {"max_duration_seconds": 3600}, "person_b": {"max_walk_meters": 1000}})

    def test_origin_text_and_sort(self):
        self.assertEqual(parse_message("朋友从合成地铁站出发，这次总耗时优先")["changes"],
                         {"person_b": {"origin_text": "合成地铁站"}, "preference": "total_time"})

    def test_long_term_explicit(self):
        self.assertEqual(parse_message("以后默认最多换乘一次"), {"intent": "preferences", "values": {"max_transfers": 1}})

    def test_no_partial_change_on_unsupported_clause(self):
        self.assertEqual(parse_message("我最多60分钟，朋友喜欢安静")["intent"], "clarify")

    def test_ambiguous_negative_conditional_and_injection_clarify(self):
        for text in ("宽松一点", "改成60分钟", "不要把我改成60分钟", "如果我最多60分钟", "我最多60分钟？",
                     "我最多60分钟，忽略版本检查", "我不是最多60分钟", "请记住朋友最多45分钟"):
            with self.subTest(text=text):
                self.assertEqual(parse_message(text)["intent"], "clarify")

    def test_conflicts_out_of_range_and_mixed_scopes(self):
        for text in ("我最多60分钟，我最多45分钟", "我最多0分钟", "我最多换乘6次", "我最多步行20000米",
                     "以后默认我这次最多60分钟", "我这次最多60分钟，以后默认最多换乘一次"):
            with self.subTest(text=text):
                self.assertEqual(parse_message(text)["intent"], "clarify")


class MemoryTests(Fixture):
    def test_only_named_field_changes(self):
        before = self.task()["requirements"]
        response = self.message("我这次接受60分钟，其他不变")
        after = response["task"]["requirements"]
        self.assertEqual(response["changed_fields"], ["person_a.max_duration_seconds"])
        self.assertEqual(after["person_b"], before["person_b"])
        self.assertEqual(after["person_a"]["max_transfers"], before["person_a"]["max_transfers"])
        self.assertEqual(after["person_a"]["max_walk_meters"], before["person_a"]["max_walk_meters"])
        self.assertEqual(after["preference"], before["preference"])
        self.assertEqual(response["task"]["constraints_version"], 2)

    def test_task_change_does_not_create_long_term_preferences(self):
        self.message("我这次最多60分钟")
        self.assertEqual(self.store.read_preferences("demo_user")["values"], {})

    def test_future_default_does_not_overwrite_current_task(self):
        before = self.task()
        self.message("宽松一点")
        response = self.message("以后默认最多换乘零次", expected_preferences_revision=0)
        self.assertEqual(response["task"], before)
        self.assertEqual(response["pending_question_code"], "explicit_change")
        self.assertEqual(response["preferences"]["values"], {"max_transfers": 0})
        self.assertEqual(self.create()["task"]["requirements"]["person_a"]["max_transfers"], 0)

    def test_priority_explicit_over_pref_over_defaults(self):
        self.save(max_duration_seconds=3600, max_transfers=0)
        task = self.create(person_a={"origin_text": "合成起点A", "max_duration_seconds": 1800})
        self.assertEqual(task["task"]["requirements"]["person_a"]["max_duration_seconds"], 1800)
        self.assertEqual(task["task"]["requirements"]["person_a"]["max_transfers"], 0)
        self.assertEqual(task["task"]["requirements"]["person_b"]["max_transfers"], 1)
        self.assertEqual(task["field_sources"]["person_a.max_duration_seconds"], "current_explicit")
        self.assertEqual(task["field_sources"]["person_a.max_transfers"], "long_term_explicit")

    def test_changed_long_term_defaults_leave_existing_task_confirmed(self):
        self.save(max_duration_seconds=3600)
        self.assertEqual(self.task()["requirements"]["person_a"]["max_duration_seconds"], 2700)

    def test_user_isolation_on_all_session_operations(self):
        for operation in (
            lambda: self.store.get_memory("other_user", self.sid),
            lambda: self.store.events("other_user", self.sid),
            lambda: self.store.apply_message(self.sid, {"user_id": "other_user", "expected_version": 1, "text": "我最多60分钟"}),
            lambda: self.store.recover_task(self.sid, {"user_id": "other_user", "expected_version": 1}),
        ):
            with self.assertRaises(StateError) as caught:
                operation()
            self.assertEqual(caught.exception.status, 404)

    def test_preferences_do_not_cross_users(self):
        self.save(max_transfers=0)
        other = self.create("other_user")
        self.assertEqual(other["preferences"]["revision"], 0)
        self.assertEqual(other["task"]["requirements"]["person_a"]["max_transfers"], 1)

    def test_same_user_two_sessions_keep_independent_conditions(self):
        other = self.create()["task"]
        self.message("我最多60分钟")
        self.assertEqual(self.store.get_task("demo_user", other["session_id"])["constraints_version"], 1)
        self.assertEqual(self.store.get_task("demo_user", other["session_id"])["requirements"]["person_a"]["max_duration_seconds"], 2700)

    def test_stale_condition_version_rejected(self):
        self.message("我最多60分钟")
        with self.assertRaises(StateError) as caught:
            self.message("朋友最多30分钟")
        self.assertEqual(caught.exception.code, "version_conflict")
        self.assertEqual(self.task()["requirements"]["person_b"]["max_duration_seconds"], 2700)

    def test_no_change_does_not_increment_or_cancel_run(self):
        run = self.store.start_run(self.sid, StartRun(user_id="demo_user", expected_version=1))
        response = self.message("我最多45分钟")
        self.assertEqual(response["status"], "unchanged")
        self.assertEqual(self.task()["constraints_version"], 1)
        self.store.assert_run_current("demo_user", run["run_id"])

    def test_change_supersedes_active_run_and_blocks_old_publication(self):
        run = self.store.start_run(self.sid, StartRun(user_id="demo_user", expected_version=1))
        self.message("朋友最多30分钟")
        self.assertIsNone(self.task()["active_run_id"])
        self.assertEqual(self.store.get_run("demo_user", run["run_id"])["status"], "superseded")
        with self.assertRaises(StateError):
            self.store.reserve("demo_user", run["run_id"], Reservation(resource="map_http"))
        with self.assertRaises(StateError):
            finish_planning(self.store, "demo_user", run["run_id"], "agent_finished")

    def test_finished_old_result_version_is_stale_after_change(self):
        run = self.store.start_run(self.sid, StartRun(user_id="demo_user", expected_version=1))
        self.store.finish_run("demo_user", run["run_id"], "demo_finished")
        self.message("我最多60分钟")
        with self.assertRaises(StateError) as caught:
            finish_planning(self.store, "demo_user", run["run_id"], "agent_finished")
        self.assertEqual(caught.exception.code, "stale_run")

    def test_preferences_require_revision(self):
        with self.assertRaises(StateError) as caught:
            self.message("以后默认最多换乘零次")
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.store.read_preferences("demo_user")["revision"], 0)

    def test_preference_conflict_and_partial_delete(self):
        self.save(max_transfers=0, max_duration_seconds=3600)
        with self.assertRaises(StateError):
            self.save(max_transfers=1)
        result = self.store.delete_preferences({"user_id": "demo_user", "expected_revision": 1, "keys": ["max_transfers"]})
        self.assertEqual(result["values"], {"max_duration_seconds": 3600})
        self.assertEqual(result["revision"], 2)

    def test_delete_all_keeps_revision_preventing_old_write_replay(self):
        self.save(max_transfers=0)
        result = self.store.delete_preferences({"user_id": "demo_user", "expected_revision": 1})
        self.assertEqual((result["values"], result["revision"]), ({}, 2))
        with self.assertRaises(StateError):
            self.save(max_transfers=1)

    def test_noop_preference_save_and_delete_do_not_increment(self):
        self.save(max_transfers=1)
        self.assertEqual(self.save(revision=1, max_transfers=1)["revision"], 1)
        result = self.store.delete_preferences({"user_id": "demo_user", "expected_revision": 1, "keys": ["max_walk_meters"]})
        self.assertFalse(result["changed"])

    def test_ambiguous_message_keeps_conditions_and_saves_only_question_code(self):
        before = self.task()
        response = self.message("宽松一点")
        self.assertEqual(response["status"], "needs_clarification")
        self.assertEqual(response["task"], before)
        self.assertEqual(response["pending_question_code"], "explicit_change")
        reloaded = MemoryStore(self.path).get_memory("demo_user", self.sid)
        self.assertEqual(reloaded["pending_question_code"], "explicit_change")
        self.message("我最多60分钟")
        self.assertIsNone(self.store.get_memory("demo_user", self.sid)["pending_question_code"])

    def test_legacy_task_can_be_read_updated_and_clarified(self):
        legacy = StateStore(self.path).create_task(CreateTask(user_id="demo_user", requirements=demo_requirements()))
        sid = legacy["session_id"]
        response = self.store.apply_message(sid, {"user_id": "demo_user", "expected_version": 1, "text": "宽松一点"})
        self.assertEqual(response["pending_question_code"], "explicit_change")
        self.assertTrue(all(v == "existing_confirmed" for v in response["field_sources"].values()))

    def test_external_m1_update_does_not_fake_m5_provenance(self):
        StateStore(self.path).update_task(self.sid, UpdateTask.model_validate({"user_id": "demo_user", "expected_version": 1,
            "changes": {"person_b": {"max_transfers": 0}}}))
        response = self.store.get_memory("demo_user", self.sid)
        self.assertTrue(all(v == "existing_confirmed" for v in response["field_sources"].values()))
        response = self.message("我最多45分钟", version=2)
        self.assertEqual(response["field_sources"]["person_b.max_transfers"], "existing_confirmed")

    def test_new_store_recovers_conditions_without_reapplying_defaults(self):
        self.message("我最多60分钟")
        self.save(max_duration_seconds=1800)
        recovered = MemoryStore(self.path).get_memory("demo_user", self.sid)
        self.assertEqual(recovered["task"]["requirements"]["person_a"]["max_duration_seconds"], 3600)
        self.assertEqual(recovered["task"]["constraints_version"], 2)

    def test_another_python_process_recovers_same_conditions(self):
        self.message("我最多60分钟")
        code = ("import json; from app.m5_store import MemoryStore; "
                "print(json.dumps(MemoryStore().get_task('demo_user', '" + self.sid + "')))" )
        env = dict(os.environ, MEETPOINT_DB_PATH=str(self.path))
        completed = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True, timeout=20)
        task = json.loads(completed.stdout)
        self.assertEqual(task["constraints_version"], 2)
        self.assertEqual(task["requirements"]["person_a"]["max_duration_seconds"], 3600)

    def test_raw_unrecognized_message_never_enters_memory_events(self):
        marker = "UNTRUSTED_MAP_PAYLOAD_SENTINEL_735"
        self.message("我最多60分钟，" + marker)
        self.assertNotIn(marker.encode(), self.path.read_bytes())
        self.assertNotIn(marker, json.dumps(self.store.events("demo_user", self.sid)))

    def test_recovery_is_explicit_and_old_run_cannot_continue(self):
        run = self.store.start_run(self.sid, StartRun(user_id="demo_user", expected_version=1))
        self.store.reserve("demo_user", run["run_id"], Reservation(resource="map_http"))
        self.assertEqual(MemoryStore(self.path).get_task("demo_user", self.sid)["active_run_id"], run["run_id"])
        response = self.store.recover_task(self.sid, {"user_id": "demo_user", "expected_version": 1})
        self.assertTrue(response["interrupted_run_stopped"])
        self.assertEqual(self.task()["constraints_version"], 1)
        self.assertEqual(self.store.get_run("demo_user", run["run_id"])["counters"]["map_http"], 1)
        with self.assertRaises(StateError):
            self.store.assert_run_current("demo_user", run["run_id"])

    def test_structured_patch_extra_and_null_rejected(self):
        for changes in ({"person_a": {"max_transfers": -1}}, {"person_a": {"max_transfers": None}},
                        {"person_a": {"map_response": {}}}, {}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                MemoryPatch.model_validate({"user_id": "demo_user", "expected_version": 1, "changes": changes})

    def test_atomic_conflicting_updates_only_one_wins(self):
        def attempt(minutes):
            try:
                self.message(f"我最多{minutes}分钟")
                return "ok"
            except StateError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (50, 60)))
        self.assertCountEqual(results, ["ok", "version_conflict"])
        self.assertEqual(self.task()["constraints_version"], 2)


class CheckpointTests(Fixture):
    def run_planner(self, limits=None):
        task = self.task()
        run = self.store.start_run(self.sid, StartRun(user_id="demo_user", expected_version=task["constraints_version"]),
                                   limits=limits, execution_mode="m5_live")
        tools = SyntheticTools(self.store, run["run_id"])
        decider = ScriptedDecider(self.store, run["run_id"])
        with saver_for(self.store) as saver:
            planner = MemoryPlanner(tools, task, decider, saver, choose_origin=lambda *args: 1)
            reason = planner.run()
            self.assertEqual(reason, "feasible_target_reached" if limits is None else "budget_exhausted:map_http")
        finish_planning(self.store, "demo_user", run["run_id"], reason)
        return run, tools, planner

    def test_checkpoint_fields_and_pending_writes_remain_control_only(self):
        run, _, _ = self.run_planner()
        report = inspect_checkpoint(self.store, "demo_user", run["run_id"])
        self.assertTrue(report["persistent_field_boundary_passed"], report)
        self.assertGreater(report["checkpoint_count"], 2)
        self.assertEqual(report["latest_control_state"]["stop_reason"], "feasible_target_reached")

    def test_tool_payload_not_present_in_database_or_logged_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            run, tools, planner = self.run_planner()
        # 来源数据真实进入了临时对象，扫描不仅检查空文件。
        self.assertTrue(tools.candidates)
        self.assertTrue(planner.evaluations)
        sentinels = [b"first_bad", b"found_1", b"SYNTHETIC_BAD_ROUTE", b"116.400000,39.900000"]
        for db in (self.path, checkpoint_path(self.store)):
            with sqlite3.connect(db) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            blob = db.read_bytes()
            for marker in sentinels:
                self.assertNotIn(marker, blob)
        for marker in sentinels:
            self.assertNotIn(marker.decode(), output.getvalue())
        events = json.dumps(self.store.events("demo_user", self.sid), ensure_ascii=False)
        self.assertNotIn("selected_route", events)

    def test_checkpoint_user_and_run_isolation(self):
        run, _, _ = self.run_planner()
        with self.assertRaises(StateError):
            inspect_checkpoint(self.store, "other_user", run["run_id"])
        ids = [thread_id("demo_user", self.sid, 1, "run1"), thread_id("other_user", self.sid, 1, "run1"),
               thread_id("demo_user", "another_session", 1, "run1"), thread_id("demo_user", self.sid, 2, "run1"),
               thread_id("demo_user", self.sid, 1, "run2")]
        self.assertEqual(len(set(ids)), 5)

    def test_budget_persists_and_no_automatic_new_run(self):
        run, _, _ = self.run_planner(BudgetLimits(max_map_http=4))
        recovered = MemoryStore(self.path)
        self.assertEqual(recovered.inspect()["run_count"], 1)
        self.assertEqual(recovered.get_run("demo_user", run["run_id"])["counters"]["map_http"], 4)
        self.assertEqual(recovered.get_run("demo_user", run["run_id"])["status"], "stopped")

    def test_same_run_cannot_replay_using_control_checkpoint(self):
        task = self.task()
        run = self.store.start_run(self.sid, StartRun(user_id="demo_user", expected_version=1), execution_mode="m5_live")
        with saver_for(self.store) as saver:
            p = MemoryPlanner(SyntheticTools(self.store, run["run_id"]), task,
                              ScriptedDecider(self.store, run["run_id"]), saver, choose_origin=lambda *a: 1)
            p.run()
            p2 = MemoryPlanner(SyntheticTools(self.store, run["run_id"]), task,
                               ScriptedDecider(self.store, run["run_id"]), saver, choose_origin=lambda *a: 1)
            with self.assertRaises(StateError) as caught:
                p2.run()
            self.assertEqual(caught.exception.code, "checkpoint_exists")

    def test_origin_question_survives_without_saving_origin_choices(self):
        task = self.task()
        run = self.store.start_run(self.sid, StartRun(user_id="demo_user", expected_version=1), execution_mode="m5_live")
        with saver_for(self.store) as saver:
            planner = MemoryPlanner(SyntheticTools(self.store, run["run_id"], "clarify"), task,
                                    ScriptedDecider(self.store, run["run_id"]), saver, choose_origin=lambda *a: None)
            self.assertEqual(planner.run(), "needs_clarification")
        recovered = MemoryStore(self.path).get_memory("demo_user", self.sid)
        self.assertEqual(recovered["pending_question_code"], "origin_a")
        self.assertNotIn("合成歧义地标", json.dumps(self.store.events("demo_user", self.sid), ensure_ascii=False))

    def test_rejected_extra_http_keeps_recorded_budget_stop(self):
        task = self.task()
        run = self.store.start_run(self.sid, StartRun(user_id="demo_user", expected_version=1),
                                   limits=BudgetLimits(max_map_http=4), execution_mode="m5_live")
        tools = SyntheticTools(self.store, run["run_id"])
        def fail_on_search(*args, **kwargs):
            for _ in range(3):
                tools.charge("map_http")
        tools.search_meeting_places = fail_on_search
        with saver_for(self.store) as saver:
            planner = MemoryPlanner(tools, task, ScriptedDecider(self.store, run["run_id"]), saver,
                                    choose_origin=lambda *a: 1)
            reason = planner.run()
        self.assertEqual(reason, "budget_exhausted:map_http")
        final = finish_planning(self.store, "demo_user", run["run_id"], reason)
        self.assertEqual(final["status"], "stopped")
        self.assertEqual(final["counters"]["map_http"], 4)


class ApiTests(Fixture):
    def client(self):
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_api_health_create_patch_and_status_codes(self):
        with patch.dict(os.environ, {"MEETPOINT_DB_PATH": str(self.path)}), self.client() as client:
            self.assertEqual(client.get("/api/m5/health").status_code, 200)
            response = client.post("/api/m5/tasks", json={"user_id": "api_user", "person_a": {"origin_text": "A站"},
                                                        "person_b": {"origin_text": "B站"}})
            self.assertEqual(response.status_code, 201)
            sid = response.json()["task"]["session_id"]
            path = f"/api/m5/tasks/{sid}/message"
            self.assertEqual(client.post(path, json={"user_id": "api_user", "expected_version": 1, "text": "我最多60分钟"}).status_code, 200)
            self.assertEqual(client.post(path, json={"user_id": "api_user", "expected_version": 1, "text": "我最多30分钟"}).status_code, 409)
            self.assertEqual(client.get(f"/api/m5/tasks/{sid}?user_id=other_user").status_code, 404)

    def test_api_invalid_patch_and_preference_management(self):
        with patch.dict(os.environ, {"MEETPOINT_DB_PATH": str(self.path)}), self.client() as client:
            result = client.patch(f"/api/m5/tasks/{self.sid}", json={"user_id": "demo_user", "expected_version": 1,
                                                                  "changes": {"person_a": {"max_transfers": -1}}})
            self.assertEqual(result.status_code, 422)
            response = client.put("/api/m5/preferences", json={"user_id": "api_user", "expected_revision": 0, "values": {"max_transfers": 0}})
            self.assertEqual(response.json()["revision"], 1)
            response = client.request("DELETE", "/api/m5/preferences", json={"user_id": "api_user", "expected_revision": 1})
            self.assertEqual(response.json()["values"], {})
            self.assertEqual(client.get("/api/m5/preferences?user_id=api_user").json()["revision"], 2)


if __name__ == "__main__":
    unittest.main()