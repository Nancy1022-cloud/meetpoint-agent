"""结构化字段的事实引用协议；不声称覆盖任意自由文本。"""


FIELDS = (("duration_seconds","s","seconds"), ("walk_meters","m","walk"),
          ("transfers","count","transfers"))


def required_claims(row):
    claims = []
    for index, rec in enumerate((row.get("result") or {}).get("recommendations", [])):
        cid = rec["candidate"]["poi_id"]
        for field in ("poi_id", "name"):
            claims.append({"claim_id":f"{index}:place:{field}", "candidate_id":cid,
                "role":None, "route_id":None, "field":field,
                "value":rec["candidate"].get(field), "unit":"text"})
        for role in ("a","b"):
            route = rec.get(role,{}).get("selected_route") or {}
            for field, unit, _ in FIELDS:
                claims.append({"claim_id":f"{index}:{role}:{field}", "candidate_id":cid,
                    "role":role, "route_id":route.get("route_id"), "field":field,
                    "value":route.get(field), "unit":unit})
    return claims


def bind_claims(row):
    """统一输出适配；读真实工具返回注册表，不从预期答案生成引用。"""
    evidence = row["telemetry"]["evidence"]
    claims = required_claims(row)
    keys = ("candidate_id","role","route_id","field","value","unit")
    for claim in claims:
        matches = [e for e in evidence if all(e.get(k)==claim.get(k) for k in keys)]
        claim["evidence_ids"] = [matches[0]["evidence_id"]] if matches else []
    row["claims"] = claims
    return row


def score_citations(row, case):
    required = {c["claim_id"]:c for c in required_claims(row)}
    evidence = {e["evidence_id"]:e for e in row["telemetry"].get("evidence",[])}
    tools = {t["attempt_id"]:t for t in row["telemetry"].get("tools",[])}
    fixtures = {c["id"]:c for c in case.get("candidates",[])}
    cited, supported, covered, failures, seen = 0, 0, set(), [], set()
    keys = ("candidate_id","role","route_id","field","value","unit")
    for claim in row.get("claims",[]):
        cid = claim.get("claim_id")
        refs = claim.get("evidence_ids",[])
        duplicate = cid in seen
        seen.add(cid)
        if not refs:
            continue
        cited += 1
        target = required.get(cid)
        ok = bool(target) and not duplicate and all(claim.get(k)==target.get(k) for k in keys)
        ok = ok and claim.get("value") is not None
        for ref in refs:
            e = evidence.get(ref,{})
            ok = ok and (e.get("run_id")==row["run_id"] and e.get("constraints_version")==1
                and e.get("planner_run_id")==row.get("planner_run_id")
                and all(e.get(k)==claim.get(k) for k in keys)
                and tools.get(e.get("tool_attempt_id"),{}).get("status")=="returned")
        # 再独立对照自建源值，防止输出和证据一起被篡改后自我一致。
        item = fixtures.get(claim.get("candidate_id"))
        if item is None:
            ok = False
        elif claim.get("role") is None:
            expected = item["id"] if claim.get("field")=="poi_id" else "自建商场_"+item["id"]
            ok = ok and claim.get("value")==expected
        else:
            try:
                idx = int(str(claim["route_id"]).removeprefix("synthetic_"))
                original_field = {f:k for f,_,k in FIELDS}[claim["field"]]
                expected = item[claim["role"]][idx][original_field]
                ok = ok and claim["value"]==expected
            except (KeyError,ValueError,IndexError,TypeError):
                ok = False
        if ok:
            supported += 1
            covered.add(cid)
        else:
            failures.append("unsupported_claim:"+str(cid))
    return {"cited":cited, "supported":supported, "required":len(required),
            "covered":len(covered), "failures":failures,
            "scope":"structured_recommendation_fields_only"}
