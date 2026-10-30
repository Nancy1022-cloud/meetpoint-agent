"""交付结果与搜索停止原因分开；所有“未找到”只描述本次查询范围。"""


def outcome_view(result=None, reason=None, status="completed", error=None, actual=None):
    result = result or {}
    reason = reason or result.get("termination_reason") or ""
    if status in ("superseded", "cancelled", "stopping"):
        return {"outcome": status, "stop_kind": status, "headline":
                "条件已变化，请重新规划。" if status == "superseded" else "本次已停止。"}
    if actual == "refused":
        return {"outcome": "refused", "stop_kind": "unsupported_scope", "headline": "当前请求超出支持范围。"}
    if reason.startswith("budget_exhausted:"):
        kind, message = "budget_exhausted", "已到本次执行预算上限。"
    elif reason in ("api_error", "llm_error", "model_output_invalid") or error:
        kind, message = "tool_unavailable", "本次接口或模型调用未完成。"
    elif reason in ("needs_clarification", "no_place"):
        kind, message = "needs_clarification", "请确认出发点或明确条件后继续。"
    elif reason == "data_missing" or result.get("outcome") == "data_missing":
        kind, message = "data_missing", "必需路线字段缺失，本次无法确认更多可行方案。"
    else:
        kind, message = "search_finished", "本次搜索已结束。"
    count = len(result.get("recommendations", []))
    if count:
        headline = f"已有 {count} 个证据完整且满足条件的方案。" + message
        return {"outcome": "recommended", "stop_kind": kind, "headline": headline}
    outcome = kind if kind != "search_finished" else "no_feasible_found"
    headline = message + "本次已查询范围内尚未交付可行方案。"
    return {"outcome": outcome, "stop_kind": kind, "headline": headline}
