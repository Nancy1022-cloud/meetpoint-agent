"""M3 确定性评估：此文件不调用模型、地图或数据库。"""

from app.m1_models import PersonRequirements, Requirements
from app.m2_models import Place, TransitResult


LIMIT_FIELDS = (
    ("duration_seconds", "max_duration_seconds"),
    ("walk_meters", "max_walk_meters"),
    ("transfers", "max_transfers"),
)
PARETO_FIELDS = (
    "max_duration_seconds", "total_duration_seconds", "gap_seconds",
    "total_walk_meters", "total_transfers",
)


def choose_route(result: TransitResult, limits: PersonRequirements):
    """只在完整且满足本人的三项上限的路线中，选耗时/步行/换乘最小者。"""
    checks, eligible = [], []
    for route in result.routes:
        comparisons = []
        for field, limit_field in LIMIT_FIELDS:
            value, limit = getattr(route, field), getattr(limits, limit_field)
            comparisons.append({
                "field": field, "value": value, "limit": limit,
                "passed": None if value is None else value <= limit,
            })
        passed = route.complete and all(c["passed"] is True for c in comparisons)
        checks.append({
            "route_id": route.route_id, "complete": route.complete,
            "eligible": passed, "constraints": comparisons,
            "missing_fields": list(route.missing_fields), "issues": list(route.issues),
        })
        if passed:
            eligible.append(route)

    # status 与内容矛盾、全局异常，均不允许继续作为可信的合格路线。
    inconsistent = (result.status == "no_route" and bool(result.routes)) or (
        result.status == "data_missing" and any(r.complete for r in result.routes)
    )
    selected = None
    if result.issues or inconsistent:
        status = "data_missing"
    elif result.status == "no_route":
        status = "no_route"
    elif eligible:
        selected = min(eligible, key=lambda r: (
            r.duration_seconds, r.walk_meters, r.transfers, r.route_id,
        ))
        status = "feasible"
    elif result.status == "data_missing" or not result.routes or any(
        not r.complete for r in result.routes
    ):
        # 有未知备选路线时，只能说尚不能确认可行，不能宣称所有备选均违规。
        status = "data_missing"
    else:
        status = "constraints_unsatisfied"
    return {
        "status": status, "selected_route": selected.model_dump() if selected else None,
        "route_checks": checks, "result_issues": list(result.issues),
        "inconsistent_result": inconsistent,
    }


def evaluate_candidate(place: Place, a: TransitResult, b: TransitResult,
                       requirements: Requirements):
    sides = {
        "a": choose_route(a, requirements.person_a),
        "b": choose_route(b, requirements.person_b),
    }
    states = {side["status"] for side in sides.values()}
    metrics = None
    if states == {"feasible"}:
        status = "feasible"
        ra, rb = sides["a"]["selected_route"], sides["b"]["selected_route"]
        ta, tb = ra["duration_seconds"], rb["duration_seconds"]
        metrics = {
            "max_duration_seconds": max(ta, tb), "total_duration_seconds": ta + tb,
            "gap_seconds": abs(ta - tb),
            "total_walk_meters": ra["walk_meters"] + rb["walk_meters"],
            "total_transfers": ra["transfers"] + rb["transfers"],
        }
    elif "no_route" in states:
        status = "no_route"
    elif "constraints_unsatisfied" in states:
        # 一侧已知所有路线违规，就足以排除此候选；仍保留另一侧的未知原因。
        status = "constraints_unsatisfied"
    else:
        status = "data_missing"
    return {"candidate": place.model_dump(), "status": status, **sides, "metrics": metrics}


def dominates(left, right):
    a, b = left["metrics"], right["metrics"]
    return all(a[k] <= b[k] for k in PARETO_FIELDS) and any(
        a[k] < b[k] for k in PARETO_FIELDS
    )


def ranking_key(item, preference):
    m = item["metrics"]
    if preference == "balanced":
        fields = ("max_duration_seconds", "gap_seconds", "total_walk_meters", "total_transfers")
    elif preference == "total_time":
        fields = ("total_duration_seconds", "max_duration_seconds",
                  "total_walk_meters", "total_transfers")
    else:
        raise ValueError("不支持的偏好模式。")
    return tuple(m[field] for field in fields) + (item["candidate"]["poi_id"],)


def rank_candidates(evaluations, preference):
    if preference not in ("balanced", "total_time"):
        raise ValueError("不支持的偏好模式。")
    ids = [item["candidate"]["poi_id"] for item in evaluations]
    if len(ids) != len(set(ids)):
        raise ValueError("同一候选不能重复参与评估和排序。")
    feasible = [item for item in evaluations if item["status"] == "feasible"]
    frontier, dominated = [], []
    for item in feasible:
        by = sorted(other["candidate"]["poi_id"] for other in feasible
                    if dominates(other, item))
        if by:
            dominated.append({"candidate_id": item["candidate"]["poi_id"], "dominated_by": by})
        else:
            frontier.append(item)
    frontier.sort(key=lambda item: ranking_key(item, preference))
    dominated.sort(key=lambda item: item["candidate_id"])
    return {
        "feasible_count": len(feasible),
        "pareto_candidate_ids": [item["candidate"]["poi_id"] for item in frontier],
        "dominated": dominated,
        "recommendations": frontier[:3],
    }