"""真实SQLite/Checkpointer/Agent图，地图与模型使用自建替身；零外部请求。"""
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.m1_models import BudgetLimits
from app.m1_store import StateError
from app.m2_models import MapToolError
from app.m4_synthetic import ScriptedDecider, SyntheticTools
from app.m5_api import router as memory_router
from app.m5_store import MemoryStore
from app.m5_checkpoint import inspect_checkpoint
from app.m6_api import router
from app.m6_models import OriginChoice, WebPlan
from app.m6_runtime import JobManager


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memory.sqlite3"
        self.env = patch.dict(os.environ, {"MEETPOINT_DB_PATH": str(self.path)})
        self.env.start()
        self.store = MemoryStore(self.path)
        self.task = self.store.create_memory_task({"user_id": "demo_user",
            "person_a": {"origin_text": "合成起点A"}, "person_b": {"origin_text": "合成起点B"}})["task"]
        self.case, self.managers = "a_time", []
        self.manager = self.make_manager()

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        self.env.stop()
        self.tmp.cleanup()

    @contextmanager
    def resources(self, store, request, rid):
        tools = SyntheticTools(store, rid, self.case)
        decider = ScriptedDecider(store, rid)
        decider.metadata = lambda: {"real_llm_attempts": 0, "decision_source": "scripted_test_double"}
        yield tools.client, decider, tools

    def make_manager(self, **kw):
        manager = JobManager(store_factory=lambda: MemoryStore(self.path),
                             resources_factory=self.resources, **kw)
        self.managers.append(manager)
        return manager

    def request(self, **kw):
        return WebPlan(user_id="demo_user", expected_version=1,
                       departure_date="2026-10-26", departure_time="10:00", **kw)

    def start(self, manager=None, request=None):
        return (manager or self.manager).start(self.task["session_id"], request or self.request())["run_id"]

    def wait_for(self, rid, predicate, manager=None):
        manager = manager or self.manager
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            d = manager.snapshot("demo_user", rid)
            if predicate(d):
                return d
            time.sleep(.005)
        self.fail("网页运行没有到达预期状态")

    def complete(self, rid, manager=None):
        manager, seen = manager or self.manager, set()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            d = manager.snapshot("demo_user", rid)
            if not d["working"]:
                return d
            p = d["pending"]
            if p and p["prompt_id"] not in seen:
                seen.add(p["prompt_id"])
                manager.choose(rid, OriginChoice(user_id="demo_user", prompt_id=p["prompt_id"], index=1))
            time.sleep(.005)
        self.fail("网页规划没有结束")

    def client(self):
        app = FastAPI()
        app.include_router(memory_router)
        app.include_router(router)
        return TestClient(app)

    def test_round_trip_with_real_graph_and_checkpoint(self):
        d = self.complete(self.start())
        self.assertEqual(d["status"], "completed", d["error"])
        self.assertEqual(d["result"]["feasible_count"], 3)
        self.assertEqual([r["action"] for r in d["result"]["rounds"]], ["middle", "toward_a"])
        self.assertTrue(d["checkpoint_audit"]["persistent_field_boundary_passed"])
        self.assertEqual(d["real_map_http_attempts"], 0)
        self.assertEqual(d["model"]["real_llm_attempts"], 0)

    def test_origins_wait_for_click_even_with_single_match(self):
        rid = self.start()
        d = self.wait_for(rid, lambda d: d["pending"] is not None)
        self.assertEqual(d["run"]["counters"]["search_rounds"], 0)
        self.assertEqual(d["pending"]["role"], "a")
        self.assertEqual(len(d["pending"]["places"]), 1)

    def test_ambiguous_origin_lists_all_choices(self):
        self.case = "clarify"
        rid = self.start()
        d = self.wait_for(rid, lambda d: d["pending"] is not None)
        self.assertEqual(len(d["pending"]["places"]), 2)

    def test_choice_must_belong_to_current_prompt(self):
        rid = self.start()
        d = self.wait_for(rid, lambda d: d["pending"] is not None)
        with self.assertRaises(StateError) as cm:
            self.manager.choose(rid, OriginChoice(user_id="demo_user", prompt_id="other", index=1))
        self.assertEqual(cm.exception.code, "stale_choice")
        with self.assertRaises(StateError) as cm:
            self.manager.choose(rid, OriginChoice(user_id="demo_user", prompt_id=d["pending"]["prompt_id"], index=2))
        self.assertEqual(cm.exception.status, 422)

    def test_choice_user_cannot_read_or_confirm_another_job(self):
        rid = self.start()
        for op in [lambda: self.manager.snapshot("someone_else", rid),
                   lambda: self.manager.cancel("someone_else", rid),
                   lambda: self.manager.choose(rid, OriginChoice(user_id="someone_else", prompt_id="x", index=1))]:
            with self.assertRaises(StateError) as cm:
                op()
            self.assertEqual(cm.exception.status, 404)

    def test_manual_cancel_stops_wait_without_publishing(self):
        rid = self.start()
        self.wait_for(rid, lambda d: d["pending"] is not None)
        self.manager.cancel("demo_user", rid)
        d = self.wait_for(rid, lambda d: not d["working"])
        self.assertEqual(d["status"], "cancelled")
        self.assertIsNone(d["result"])
        self.assertEqual(d["run"]["stop_reason"], "manual_stop")
        self.assertIsNone(self.store.get_task("demo_user", self.task["session_id"])["active_run_id"])
        self.assertTrue(d["checkpoint_audit"]["persistent_field_boundary_passed"])

    def test_timed_out_confirmation_releases_active_run(self):
        manager = self.make_manager(origin_timeout=.03)
        rid = self.start(manager)
        d = self.wait_for(rid, lambda d: not d["working"], manager)
        self.assertEqual(d["run"]["stop_reason"], "needs_clarification")
        self.assertTrue(d["confirmation_timed_out"])
        self.assertEqual(d["result"]["recommendations"], [])

    def test_map_budget_halts_with_no_partial_card(self):
        rid = self.start(request=self.request(limits=BudgetLimits(max_map_http=1)))
        d = self.complete(rid)
        self.assertEqual(d["run"]["stop_reason"], "budget_exhausted:map_http")
        self.assertEqual(d["run"]["counters"]["map_http"], 1)
        self.assertEqual(d["result"]["recommendations"], [])

    def test_same_process_does_not_queue_unbounded_jobs(self):
        self.start()
        with self.assertRaises(StateError) as cm:
            self.start()
        self.assertEqual(cm.exception.code, "web_busy")
        self.assertEqual(self.store.inspect()["run_count"], 1)

    def test_stale_task_version_does_not_start_a_run(self):
        self.store.apply_message(self.task["session_id"], {"user_id": "demo_user", "expected_version": 1,
            "text": "我这次接受60分钟"})
        with self.assertRaises(StateError) as cm:
            self.start()
        self.assertEqual(cm.exception.code, "version_conflict")
        self.assertEqual(self.store.inspect()["run_count"], 0)

    def test_condition_change_while_waiting_invalidates_job(self):
        rid = self.start()
        self.wait_for(rid, lambda d: d["pending"] is not None)
        self.store.apply_message(self.task["session_id"], {"user_id": "demo_user", "expected_version": 1,
            "text": "我这次接受60分钟"})
        d = self.wait_for(rid, lambda d: not d["working"])
        self.assertEqual(d["status"], "superseded")
        self.assertIsNone(d["result"])
        self.assertTrue(inspect_checkpoint(self.store, "demo_user", rid)["persistent_field_boundary_passed"])

    def test_finished_result_is_hidden_after_new_conditions(self):
        rid = self.start()
        self.assertTrue(self.complete(rid)["result"]["recommendations"])
        self.store.apply_message(self.task["session_id"], {"user_id": "demo_user", "expected_version": 1,
            "text": "我这次接受60分钟"})
        d = self.manager.snapshot("demo_user", rid)
        self.assertEqual(d["status"], "superseded")
        self.assertIsNone(d["result"])

    def test_memory_ambiguity_must_be_resolved_first(self):
        self.store.apply_message(self.task["session_id"], {"user_id": "demo_user", "expected_version": 1, "text": "近一点"})
        with self.assertRaises(StateError) as cm:
            self.start()
        self.assertEqual(cm.exception.code, "unresolved_change")

    def test_new_run_requeries_and_does_not_reuse_checkpoint(self):
        first = self.complete(self.start())
        second = self.complete(self.start())
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["run"]["counters"], second["run"]["counters"])
        self.assertTrue(second["checkpoint_audit"]["persistent_field_boundary_passed"])

    def test_expired_results_disappear_but_conditions_remain(self):
        manager = self.make_manager(retention_seconds=.03)
        rid = self.start(manager)
        self.complete(rid, manager)
        time.sleep(.05)
        with self.assertRaises(StateError) as cm:
            manager.snapshot("demo_user", rid)
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(self.store.get_task("demo_user", self.task["session_id"])["constraints_version"], 1)

    def test_process_restart_has_conditions_without_job_objects(self):
        rid = self.start()
        self.wait_for(rid, lambda d: d["pending"] is not None)
        second = self.make_manager()
        with self.assertRaises(StateError):
            second.snapshot("demo_user", rid)
        recovered = self.store.recover_task(self.task["session_id"], {"user_id": "demo_user", "expected_version": 1})
        self.assertTrue(recovered["interrupted_run_stopped"])
        self.assertEqual(recovered["task"]["requirements"], self.task["requirements"])

    def test_tool_failure_finishes_run_and_does_not_claim_no_route(self):
        @contextmanager
        def broken(*args):
            raise MapToolError("map_connection_error", "地图连接失败。")
            yield
        manager = self.make_manager()
        manager.resources_factory = broken
        d = self.wait_for(self.start(manager), lambda d: not d["working"], manager)
        self.assertEqual(d["status"], "failed")
        self.assertEqual(d["run"]["stop_reason"], "api_error")
        self.assertEqual(d["error"]["code"], "map_connection_error")
        self.assertIsNone(d["result"])

    def test_business_database_has_no_synthetic_map_content(self):
        self.complete(self.start())
        with self.store.connection() as conn:
            dump = '\n'.join(conn.iterdump())
        for value in ['SYNTHETIC_BAD_ROUTE', '116.300000', 'found_1', 'line_options', 'selected_route']:
            self.assertNotIn(value, dump)

    def test_api_and_single_html_are_connected(self):
        with self.client() as client, patch("app.m6_api.manager", return_value=self.manager):
            self.assertEqual(client.get('/api/m6/health').json()['milestone'], 'M6')
            page = client.get('/demo')
            self.assertEqual(page.status_code, 200)
            self.assertIn('text/html', page.headers['content-type'])
            self.assertIn('/api/m6/tasks/', page.text)
            self.assertIn('/api/m5/tasks/', page.text)
            data = client.post('/api/m6/tasks/'+self.task['session_id']+'/plan', json=self.request().model_dump())
            self.assertEqual(data.status_code, 202, data.text)
            rid = data.json()['run_id']
            self.assertEqual(client.get('/api/m6/jobs/'+rid+'?user_id=other').status_code, 404)
            self.assertEqual(client.get('/api/m6/jobs/'+rid+'?user_id=demo_user').status_code, 200)

    def test_http_422_and_409(self):
        with self.client() as client, patch("app.m6_api.manager", return_value=self.manager):
            body = self.request().model_dump()
            body['limits']['max_map_http'] = 41
            url = '/api/m6/tasks/'+self.task['session_id']+'/plan'
            self.assertEqual(client.post(url, json=body).status_code, 422)
            body = self.request().model_dump()
            body['expected_version'] = 2
            self.assertEqual(client.post(url, json=body).status_code, 409)

    def test_m5_preference_api_still_controls_new_sessions(self):
        with self.client() as client:
            response = client.put('/api/m5/preferences', json={'user_id':'demo_user', 'expected_revision':0, 'values':{'max_transfers':0}})
            self.assertEqual(response.status_code, 200)
            response = client.post('/api/m5/tasks', json={'user_id':'demo_user', 'person_a':{'origin_text':'另一个起点A'}, 'person_b':{'origin_text':'另一个起点B'}})
            self.assertEqual(response.json()['task']['requirements']['person_a']['max_transfers'], 0)
            self.assertEqual(self.store.get_task('demo_user', self.task['session_id'])['requirements']['person_a']['max_transfers'], 1)

    def test_request_calendar_and_numeric_validation(self):
        for values in [{'departure_date':'2026-02-30'}, {'departure_time':'24:00'}, {'expected_version':True}]:
            body = self.request().model_dump()
            body.update(values)
            with self.assertRaises(ValidationError):
                WebPlan.model_validate(body)


if __name__ == '__main__':
    unittest.main()