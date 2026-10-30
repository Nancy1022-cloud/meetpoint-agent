"""同一受控解析器的Memory状态保留对照；不是LLM记忆能力评测。"""

import tempfile
from pathlib import Path

from app.m1_models import Requirements
from app.m5_store import MemoryStore, flatten


USER = "demo_user"


def default_new():
    return {"op": "new", "a": {"origin_text": "合成起点A"},
            "b": {"origin_text": "合成起点B"}}


def replay(events):
    with tempfile.TemporaryDirectory(prefix="m7_memory_") as temp:
        store, sid = MemoryStore(Path(temp) / "memory.sqlite3"), None
        for event in events:
            prefs = store.read_preferences(USER)
            if event["op"] == "new":
                memory = store.create_memory_task({"user_id": USER,
                    "person_a": event["a"], "person_b": event["b"]})
                sid = memory["task"]["session_id"]
            elif event["op"] == "save":
                store.save_preferences({"user_id": USER, "expected_revision": prefs["revision"],
                                        "values": event["values"]})
            elif event["op"] == "say":
                if sid is None:
                    memory = store.create_memory_task({"user_id": USER,
                        "person_a": default_new()["a"], "person_b": default_new()["b"]})
                    sid = memory["task"]["session_id"]
                memory = store.get_memory(USER, sid)
                store.apply_message(sid, {"user_id": USER,
                    "expected_version": memory["task"]["constraints_version"],
                    "expected_preferences_revision": prefs["revision"], "text": event["text"]})
            else:
                raise ValueError("未知Memory事件")
        if sid is None:
            raise ValueError("用例没有创建任务")
        memory = store.get_memory(USER, sid)
        return {"requirements": flatten(memory["task"]["requirements"]),
                "preferences": memory["preferences"]["values"],
                "pending": memory["pending_question_code"] is not None}


def evaluate_memory(cases, recent_n=3):
    if recent_n < 1:
        raise ValueError("recent_n必须为正数")
    rows = []
    base = flatten(Requirements.model_validate({"person_a": default_new()["a"],
                                              "person_b": default_new()["b"]}).model_dump())
    for case in cases:
        expected = {"requirements": {**base, **case["expected_changes_from_defaults"]},
                    "preferences": case["expected_preferences"], "pending": case["expected_pending"]}
        for policy, events in (("structured", case["events"]),
                               ("recent_events_replay", case["events"][-recent_n:])):
            actual = replay(events)
            wrong = [k for k, value in expected["requirements"].items()
                     if actual["requirements"].get(k) != value]
            if actual["preferences"] != expected["preferences"]:
                wrong.append("long_term_preferences")
            if actual["pending"] != expected["pending"]:
                wrong.append("pending_clarification")
            rows.append({"case_id": case["id"], "title": case["title"], "policy": policy,
                         "passed": not wrong, "wrong_fields": wrong, "actual": actual,
                         "expected": expected})
    return {"record_type": "m7_memory_state_control", "recent_n": recent_n,
            "same_parser": "app.m5_parse.parse_message", "real_map_http_attempts": 0,
            "real_llm_attempts": 0, "input_token_limit": "not_applicable_no_llm",
            "scope_note": "窗口基线从默认任务重放最近N条自建用户事件；不代表所有聊天模型。"
                          "这不是原大纲的等Token、实模Memory消融实验。",
            "summary": {p: {"passed": sum(r["passed"] for r in rows if r["policy"] == p),
                            "total": len(cases)} for p in ("structured", "recent_events_replay")},
            "rows": rows}