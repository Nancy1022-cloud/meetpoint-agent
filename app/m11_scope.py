"""Extend M9 transport vocabulary without changing its historical evaluator."""
import re
from app.m1_store import now
from app.m5_models import MemoryMessage
from app.m9_scope import CAPABILITIES, scope_decision as old_scope, apply_scoped_message as old_apply

def scope_decision(text):
    # M9 already handles quoted, hypothetical and negative requests. Normalize
    # just this synonym, then use exactly the same negation/reporting logic.
    normalized = text.replace("驾车", "开车")
    normalized = re.sub(r"(?:不要|别|不用|不想|不需要)(?:改成|改为|改用|使用|采用|选择|规划)?(?:开车|自驾|骑行)", "", normalized)
    result = old_scope(normalized)
    if result["decision"] == "refuse":
        return {**result, "protocol": "m11_scope_v1"}
    compact = re.sub(r"\s+", "", normalized)
    compact = re.sub(r'[“‘"].*?[”’"]', "", compact)
    for clause in re.split(r"[，,。；;！!]", compact):
        if re.search(r"(?:不要|别|不用|不想|如果|假设|他说|她说|支持|[？?])", clause):
            continue
        if re.search(r"(?:改为|改成|改用|规划|使用|选择).{0,10}(?:开车|自驾|骑行)", clause):
            return {"decision": "refuse", "scope_reason": "transport", "reason": "unsupported_scope",
                "message": "目前仅支持公交/地铁路线，请改用公共交通。", "protocol": "m11_scope_v1"}
    return result

def apply_scoped_message(store, session_id, request):
    request = MemoryMessage.model_validate(request)
    decision = scope_decision(request.text)
    # Existing supported controlled grammar stays on the M9/M5 path.
    if decision["decision"] != "refuse":
        return old_apply(store, session_id, request)
    with store.write() as conn:
        task = store._task(conn, request.user_id, session_id)
        store.check_version(task, request.expected_version)
        store._event(conn, request.user_id, session_id, task["constraints_version"],
            "scope_refused", {"reason": decision["scope_reason"], "protocol": "m11_scope_v1"})
        return {"status": "refused", "reason": "unsupported_scope", "scope_reason": decision["scope_reason"],
            "message": decision["message"], "capabilities": CAPABILITIES,
            "changed_fields": [], "replan_required": False, **store._memory(conn, request.user_id, session_id)}
