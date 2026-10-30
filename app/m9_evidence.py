"""工具成功返回边界采证；8类字段引用验证及统一页面展示协议。"""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from app.m8_evidence import FIELDS, bind_claims, required_claims
from app.m9_outcomes import outcome_view

PROTOCOL = "m9_structured_view_v1"
KEYS = ("candidate_id", "role", "route_id", "field", "value", "unit")


class EvidenceTools:
    """只包装本次工具对象；不额外查询，不向SQLite/checkpoint写地图内容。"""
    def __init__(self, delegate, run_id, constraints_version):
        self.delegate, self.run_id, self.version = delegate, run_id, constraints_version
        self.tools, self.evidence = [], []

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def _call(self, name, *args, **kwargs):
        attempt = {"attempt_id": uuid4().hex, "name": name, "status": "started"}
        self.tools.append(attempt)
        try:
            result = getattr(self.delegate, name)(*args, **kwargs)
            attempt["status"] = "returned"
            return result, attempt
        except Exception:
            attempt["status"] = "error"
            raise

    def _add(self, attempt, cid, role, route_id, field, value, unit):
        self.evidence.append({"evidence_id": uuid4().hex, "run_id": self.run_id,
            "planner_run_id": self.run_id, "constraints_version": self.version,
            "tool_attempt_id": attempt["attempt_id"], "candidate_id": cid,
            "role": role, "route_id": route_id, "field": field,
            "value": deepcopy(value), "unit": unit,
            "observed_at": datetime.now(timezone.utc).isoformat()})

    def resolve_place(self, role, query):
        return self._call("resolve_place", role, query)[0]

    def search_meeting_places(self, center, radius=1500):
        result, attempt = self._call("search_meeting_places", center, radius=radius)
        for place in result.places:
            for field in ("poi_id", "name"):
                self._add(attempt, place.poi_id, None, None, field, getattr(place, field), "text")
        return result

    def get_transit_routes(self, role, candidate_id):
        result, attempt = self._call("get_transit_routes", role, candidate_id)
        for route in result.routes:
            for field, unit, _ in FIELDS:
                self._add(attempt, candidate_id, role, route.route_id, field, getattr(route, field), unit)
        return result

    def row(self, result):
        return {"run_id": self.run_id, "planner_run_id": self.run_id, "result": result,
                "telemetry": {"tools": deepcopy(self.tools), "evidence": deepcopy(self.evidence)}}


def same_value(a, b):
    return type(a) is type(b) and a == b


def validate_claims(row, version):
    errors = []
    result = row.get("result") or {}
    if result and result.get("constraints_version") != version:
        errors.append("stale_result_version")
    evidence_list = row.get("telemetry", {}).get("evidence", [])
    tool_list = row.get("telemetry", {}).get("tools", [])
    evidence = {e["evidence_id"]: e for e in evidence_list}
    tools = {t["attempt_id"]: t for t in tool_list}
    if len(evidence) != len(evidence_list) or len(tools) != len(tool_list):
        errors.append("duplicate_evidence_or_attempt_id")
    wanted = required_claims(row)
    claims_list = row.get("claims", [])
    claims = {c["claim_id"]: c for c in claims_list}
    if len(claims) != len(claims_list) or set(claims) != {c["claim_id"] for c in wanted}:
        errors.append("missing_extra_or_duplicate_claims")
    for target in wanted:
        claim = claims.get(target["claim_id"], {})
        refs = claim.get("evidence_ids", [])
        ok = bool(refs) and len(set(refs)) == len(refs)
        ok = ok and all(same_value(claim.get(k), target.get(k)) for k in KEYS)
        if target["role"] is not None:
            ok = ok and type(target["value"]) is int and target["value"] >= 0
        else:
            ok = ok and type(target["value"]) is str and bool(target["value"])
        for ref in refs:
            e = evidence.get(ref, {})
            tool = tools.get(e.get("tool_attempt_id"), {})
            ok = ok and (e.get("run_id") == row["run_id"]
                and e.get("planner_run_id") == row.get("planner_run_id")
                and e.get("constraints_version") == version
                and all(same_value(e.get(k), target.get(k)) for k in KEYS)
                and tool.get("status") == "returned"
                and tool.get("name") == ("get_transit_routes" if target["role"] else "search_meeting_places"))
        if not ok:
            errors.append("unsupported_claim:" + target["claim_id"])
    return sorted(set(errors))


def build_view(row, requirements=None, version=1, status="completed", error=None):
    """验证完才生成卡片；派生值只从已引用字段计算。拒绝凭输出自行补造证据。"""
    result = row.get("result") or {}
    errors = validate_claims(row, version)
    cards, seen = [], set()
    for i, rec in enumerate(result.get("recommendations", [])):
        cid = rec["candidate"]["poi_id"]
        if cid in seen or rec.get("status") != "feasible":
            errors.append("duplicate_or_infeasible_candidate")
        seen.add(cid)
        sides = {}
        for role in ("a", "b"):
            route = rec.get(role, {}).get("selected_route") or {}
            for field, cap in (("duration_seconds", "max_duration_seconds"),
                               ("walk_meters", "max_walk_meters"), ("transfers", "max_transfers")):
                value = route.get(field)
                if type(value) is not int or value < 0 or (field == "duration_seconds" and value == 0):
                    errors.append("invalid_route_value")
                if requirements and (type(value) is not int or value > requirements["person_" + role][cap]):
                    errors.append("hard_constraint:" + role + ":" + field)
            sides[role] = {k: route.get(k) for k, _, _ in FIELDS}
            sides[role]["route_id"] = route.get("route_id")
        if all(type(sides[r]["duration_seconds"]) is int for r in ("a", "b")):
            total = sides["a"]["duration_seconds"] + sides["b"]["duration_seconds"]
            gap = abs(sides["a"]["duration_seconds"] - sides["b"]["duration_seconds"])
        else:
            total = gap = None
        # M8的metrics也校验，防止正文的派生值与引用路线冲突。
        metrics = rec.get("metrics") or {}
        if metrics.get("total_duration_seconds") != total or metrics.get("gap_seconds") != gap:
            errors.append("derived_metric_mismatch")
        card_claims = [deepcopy(c) for c in row.get("claims", []) if c["claim_id"].startswith(str(i) + ":")]
        refs = {ref for c in card_claims for ref in c.get("evidence_ids", [])}
        cards.append({"candidate_id": cid, "name": rec["candidate"].get("name"), **sides,
                      "total_duration_seconds": total, "gap_seconds": gap,
                      "claims": card_claims,
                      "evidence": [deepcopy(e) for e in row["telemetry"]["evidence"] if e["evidence_id"] in refs]})
    view = outcome_view(result, row.get("reason"), status, error, row.get("actual_outcome"))
    if errors:
        view = {"outcome": "evidence_invalid", "stop_kind": "evidence_invalid",
                "headline": "推荐证据未通过校验，请重新规划。"}
    return {"protocol": PROTOCOL, "constraints_version": version, **view,
            "evidence_valid": not errors, "validation_errors": sorted(set(errors)),
            "cards": [] if errors else cards,
            "scope_note": "仅覆盖本次查询；八类字段引用追溯至工具返回，不能证明地图数据本身绝对准确。"}


def web_view(observer, result, requirements, reason):
    row = observer.row(result)
    row["reason"] = reason
    bind_claims(row)
    return build_view(row, requirements, observer.version)
