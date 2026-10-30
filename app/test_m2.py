import json
import io
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from app.config import Settings
from app.m1_models import (
    BudgetLimits, CreateTask, Requirements, Reservation, StartRun, UpdateTask,
)
from app.m1_store import StateError, StateStore
from app.m2_client import MapClient, RateGate
from app.m2 import probe
from app.m2_models import MapToolError, TransitRoute, nonnegative_int
from app.m2_parser import parse_places, parse_transits
from app.m2_tools import MapTools


def success(**kwargs):
    return {"status": "1", "infocode": "10000", **kwargs}


def poi(poi_id="mall1", typecode="060101"):
    return {"id": poi_id, "name": "自建测试商场", "location": "116.300000,39.900000",
            "citycode": "010", "typecode": typecode, "address": "自建测试地址"}


def segment(walk=100, lines=("测试线路甲",)):
    return {"walking": {"distance": str(walk)},
            "bus": {"buslines": [{"name": name} for name in lines]}}


def transit(segments=None, **kwargs):
    raw = {"cost": {"duration": "1800"},
           "segments": segments if segments is not None else [segment(), segment(200)]}
    raw.update(kwargs)
    return success(route={"transits": [raw]})


class ParserTests(unittest.TestCase):
    def test_missing_and_invalid_numbers_stay_unknown(self):
        for value in (None, [], "", "-1", "NaN", "1.5", True, 2.5):
            self.assertIsNone(nonnegative_int(value))
        self.assertEqual(nonnegative_int("0"), 0)

    def test_empty_transits_is_no_route(self):
        self.assertEqual(parse_transits(success(route={"transits": []})).status, "no_route")

    def test_missing_transits_is_data_missing(self):
        self.assertEqual(parse_transits(success(route={})).status, "data_missing")

    def test_duration_must_use_v5_cost_not_v3(self):
        data = transit(cost={}, duration="100")
        result = parse_transits(data)
        self.assertIsNone(result.routes[0].duration_seconds)
        self.assertEqual(result.status, "data_missing")

    def test_normalize_seconds_meters_and_transfers(self):
        r = parse_transits(transit()).routes[0]
        self.assertEqual((r.duration_seconds, r.walk_meters, r.boardings, r.transfers),
                         (1800, 300, 2, 1))
        self.assertTrue(r.complete)

    def test_alternative_buslines_count_as_one_boarding(self):
        r = parse_transits(transit([segment(lines=("甲", "乙"))])).routes[0]
        self.assertEqual((r.boardings, r.transfers), (1, 0))
        self.assertEqual(r.line_options, [["甲", "乙"]])

    def test_last_walk_segment_is_included_but_not_a_transfer(self):
        r = parse_transits(transit([segment(150), segment(250, ())])).routes[0]
        self.assertEqual((r.walk_meters, r.transfers), (400, 0))

    def test_missing_walking_is_not_zero(self):
        data = transit()
        del data["route"]["transits"][0]["segments"][0]["walking"]
        self.assertIsNone(parse_transits(data).routes[0].walk_meters)

    def test_explicit_total_walk_can_fill_missing_segments(self):
        data = transit(walking_distance="500")
        del data["route"]["transits"][0]["segments"][0]["walking"]
        self.assertEqual(parse_transits(data).routes[0].walk_meters, 500)

    def test_disagreeing_walk_totals_are_flagged(self):
        r = parse_transits(transit(walking_distance="999")).routes[0]
        self.assertIn("walking_total_mismatch", r.issues)
        self.assertFalse(r.complete)

    def test_missing_segments_cannot_mean_zero_transfers(self):
        r = parse_transits(transit(segments=[])).routes[0]
        self.assertIsNone(r.transfers)

    def test_rail_or_taxi_is_not_silently_ignored(self):
        data = transit()
        data["route"]["transits"][0]["segments"][0]["railway"] = {"id": "synthetic"}
        self.assertEqual(parse_transits(data).status, "data_missing")

    def test_pure_walk_has_no_transfer(self):
        r = parse_transits(transit([segment(300, ())])).routes[0]
        self.assertEqual((r.boardings, r.transfers, r.walk_meters), (0, 0, 300))

    def test_default_route_is_not_complete(self):
        self.assertFalse(TransitRoute(route_id="missing").complete)

    def test_one_complete_route_does_not_hide_bad_route(self):
        data = transit()
        data["route"]["transits"].append({})
        r = parse_transits(data)
        self.assertEqual(r.status, "ok")
        self.assertFalse(r.routes[1].complete)

    def test_places_missing_array_and_empty_array_differ(self):
        self.assertEqual(parse_places(success()).status, "data_missing")
        self.assertEqual(parse_places(success(pois=[])).status, "no_place")

    def test_mall_subtype_and_city_filters(self):
        foreign = poi("other_city")
        foreign["citycode"] = "021"
        r = parse_places(success(pois=[poi(), poi("shop", "060000"), foreign]), mall_only=True)
        self.assertEqual([p.poi_id for p in r.places], ["mall1"])
        self.assertEqual((r.rejected_category, r.rejected_city), (1, 1))

    def test_missing_coordinate_or_type_is_rejected(self):
        bad, unknown = poi("bad"), poi("unknown")
        bad["location"] = ""
        unknown.pop("typecode")
        r = parse_places(success(pois=[bad, unknown]), mall_only=True)
        self.assertEqual(r.status, "data_missing")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.store = StateStore(self.path / "state.sqlite3")
        self.task = self.store.create_task(CreateTask.model_validate({
            "user_id": "demo_user", "requirements": {
                "person_a": {"origin_text": "测试起点A"},
                "person_b": {"origin_text": "测试起点B"},
            },
        }))
        self.requests, self.responses, self.clients = [], [], []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.tmp.cleanup()

    def make_client(self, limit=40):
        run = self.store.start_run(self.task["session_id"], StartRun(
            user_id="demo_user", expected_version=1,
        ), BudgetLimits(max_map_http=limit), execution_mode="m2_live")

        def handler(request):
            self.requests.append(request)
            response = self.responses.pop(0)
            if response == "timeout":
                raise httpx.ReadTimeout("synthetic", request=request)
            status, payload = response
            return httpx.Response(status, json=payload)

        client = MapClient(Settings(amap_web_key="synthetic_not_a_real_key", _env_file=None),
                           self.store, "demo_user", run["run_id"],
                           transport=httpx.MockTransport(handler),
                           gate=RateGate(self.path / "gate.sqlite3", 0))
        self.clients.append(client)
        return client

    def counters(self, client):
        return self.store.get_run("demo_user", client.run_id)["counters"]

    def test_first_search_is_not_double_charged(self):
        c = self.make_client()
        self.responses = [(200, success(pois=[]))]
        c.get("/v5/place/around", {})
        self.assertEqual((self.counters(c)["search_rounds"], self.counters(c)["map_http"]), (1, 1))

    def test_search_retry_costs_two_http_but_one_round(self):
        c = self.make_client()
        self.responses = [(429, {}), (200, success(pois=[]))]
        with patch("app.m2_client.time.sleep"):
            c.get("/v5/place/around", {})
        self.assertEqual((self.counters(c)["search_rounds"], self.counters(c)["map_http"]), (1, 2))

    def test_timeout_retries_once(self):
        c = self.make_client()
        self.responses = ["timeout", "timeout", (200, success())]
        with patch("app.m2_client.time.sleep"), self.assertRaises(MapToolError) as ctx:
            c.get("/v5/place/text", {})
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertEqual(len(self.requests), 2)

    def test_5xx_retries_once(self):
        c = self.make_client()
        self.responses = [(503, {}), (200, success())]
        with patch("app.m2_client.time.sleep"):
            c.get("/v5/place/text", {})
        self.assertEqual(c.retries_started, 1)

    def test_invalid_key_does_not_retry_or_expose_key(self):
        c = self.make_client()
        self.responses = [(200, {"status": "0", "infocode": "10009", "info": c.key})]
        with self.assertRaises(MapToolError) as ctx:
            c.get("/v5/place/text", {})
        self.assertEqual(ctx.exception.code, "amap_auth_error")
        self.assertNotIn(c.key, json.dumps(ctx.exception.as_dict()))
        self.assertEqual(len(self.requests), 1)

    def test_daily_quota_does_not_retry(self):
        c = self.make_client()
        self.responses = [(200, {"status": "0", "infocode": "10003"})]
        with self.assertRaises(MapToolError) as ctx:
            c.get("/v5/place/text", {})
        self.assertEqual(ctx.exception.code, "amap_quota_exhausted")
        self.assertEqual(len(self.requests), 1)

    def test_budget_denial_prevents_retry_and_extra_request(self):
        c = self.make_client(limit=1)
        self.responses = [(500, {}), (200, success())]
        with patch("app.m2_client.time.sleep"), self.assertRaises(StateError) as ctx:
            c.get("/v5/place/text", {})
        self.assertEqual(ctx.exception.code, "budget_exhausted")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.counters(c)["map_http"], 1)

    def test_parameter_error_does_not_become_no_route(self):
        c = self.make_client()
        self.responses = [(200, {"status": "0", "infocode": "20000"})]
        with self.assertRaises(MapToolError) as ctx:
            c.get("/v5/direction/transit/integrated", {})
        self.assertEqual(ctx.exception.code, "amap_parameter_error")

    def test_old_run_stops_before_another_request(self):
        c = self.make_client()
        self.store.update_task(self.task["session_id"], UpdateTask.model_validate({
            "user_id": "demo_user", "expected_version": 1,
            "changes": {"person_a": {"max_transfers": 2}},
        }))
        with self.assertRaises(StateError):
            c.get("/v5/place/text", {})
        self.assertEqual(len(self.requests), 0)

    def test_condition_change_during_request_rejects_its_result(self):
        c = self.make_client()

        def handler(request):
            self.store.update_task(self.task["session_id"], UpdateTask.model_validate({
                "user_id": "demo_user", "expected_version": 1,
                "changes": {"person_a": {"max_transfers": 2}},
            }))
            return httpx.Response(200, json=success(pois=[poi()]))

        c.http.close()
        c.http = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(StateError) as ctx:
            c.get("/v5/place/text", {})
        self.assertEqual(ctx.exception.code, "stale_run")
        self.assertEqual(c.attempts_started, 1)

    def test_unverified_candidate_is_blocked_before_http(self):
        c = self.make_client()
        tools = MapTools(c, "2026-10-25", "10:00")
        with self.assertRaises(MapToolError):
            tools.get_transit_routes("a", "invented_id")
        self.assertEqual(len(self.requests), 0)

    def test_same_run_search_deduplicates_and_caps_five(self):
        c = self.make_client()
        tools = MapTools(c, "2026-10-25", "10:00")
        self.responses = [(200, success(pois=[poi("origin")])),
                          (200, success(pois=[poi(str(i)) for i in range(8)])),
                          (200, success(pois=[poi(str(i)) for i in range(8)]))]
        tools.resolve_place("a", "测试")
        for role in ("a", "b"):
            tools.resolutions[role] = tools.resolutions["a"]
            tools.confirm_origin(role, "origin")
        first = tools.search_meeting_places(tools.midpoint())
        second = tools.search_meeting_places(tools.midpoint())
        self.assertEqual((len(first.places), len(second.places)), (5, 3))
        self.assertEqual(self.counters(c)["candidates_total"], 8)

    def test_shared_gate_serializes_concurrent_callers(self):
        path = self.path / "shared.sqlite3"
        RateGate(path, 0.03)
        starts = []

        def request(_):
            gate = RateGate(path, 0.03)
            with gate.slot() as conn:
                gate.mark_started(conn)
                starts.append(time.time())

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(request, range(3)))
        starts.sort()
        self.assertTrue(all(b - a >= 0.025 for a, b in zip(starts, starts[1:])))

    def test_storage_has_counters_but_no_map_results(self):
        c = self.make_client()
        self.responses = [(200, success(pois=[poi("distinct_synthetic_poi")]))]
        c.get("/v5/place/text", {})
        with sqlite3.connect(self.store.path) as conn:
            dump = "\n".join(conn.iterdump())
        self.assertNotIn("distinct_synthetic_poi", dump)
        self.assertNotIn(c.key, dump)
        self.assertIn("m2_live", dump)

    def test_whole_probe_checks_both_origins_search_and_two_routes(self):
        c = self.make_client()
        self.responses = [(200, success(pois=[poi("a")])),
                          (200, success(pois=[poi("b")])),
                          (200, success(pois=[poi("mall")])),
                          (200, transit()), (200, transit())]
        tools = MapTools(c, "2026-10-25", "10:00")
        with patch("builtins.input", side_effect=["1", "1"]), redirect_stdout(io.StringIO()):
            reason, result = probe(tools, self.task)
        self.assertEqual((reason, result["complete_route_pairs"]), ("tools_checked", 1))
        self.assertEqual(self.counters(c)["map_http"], 5)
        self.assertEqual(self.counters(c)["llm_calls"], 0)
        self.assertEqual(result["pairs"][0]["candidate"]["poi_id"], "mall")
        params = dict(self.requests[-1].url.params)
        self.assertEqual((params["city1"], params["city2"], params["time"], params["show_fields"]),
                         ("010", "010", "10-00", "cost"))
        finished = self.store.finish_run("demo_user", c.run_id, reason)
        self.assertEqual(finished["stop_reason"], "tools_checked")


if __name__ == "__main__":
    unittest.main(verbosity=2)