import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.m1_store import StateError
from app.m4_agent import B1Planner
from app.m4_models import ControlState


def checkpoint_path(store):
    return Path(str(store.path) + ".checkpoints.sqlite3")


def thread_id(user_id, session_id, version, run_id):
    # 纳入用户、会话、条件版本和运行，不复用上次预算或未完成的控制节点。
    return hashlib.sha256(json.dumps([user_id, session_id, version, run_id]).encode()).hexdigest()


@contextmanager
def saver_for(store):
    with SqliteSaver.from_conn_string(str(checkpoint_path(store))) as saver:
        saver.setup()
        yield saver


class MemoryPlanner(B1Planner):
    def __init__(self, tools, task, decider, saver, choose_origin=None, on_event=None):
        self.checkpointer = saver
        super().__init__(tools, task, decider, choose_origin, on_event)

    def build_graph(self):
        graph = StateGraph(ControlState)
        for name in ("prepare", "search", "observe", "decide"):
            graph.add_node(name, self.guarded(getattr(self, name)))
        graph.add_edge(START, "prepare")
        graph.add_conditional_edges("prepare", lambda s: "end" if s["stop_reason"] else "search",
                                    {"end": END, "search": "search"})
        graph.add_edge("search", "observe")
        graph.add_conditional_edges("observe", lambda s: "end" if s["stop_reason"] else "decide",
                                    {"end": END, "decide": "decide"})
        graph.add_conditional_edges("decide", lambda s: "end" if s["stop_reason"] else "search",
                                    {"end": END, "search": "search"})
        return graph.compile(checkpointer=self.checkpointer)

    def run(self):
        config = {"recursion_limit": 40, "configurable": {"thread_id": thread_id(
            self.user_id, self.task["session_id"], self.task["constraints_version"], self.run_id)}}
        # 同一run不得以丢失临时工具上下文的新对象重新进入图。
        if self.checkpointer.get_tuple(config) is not None:
            raise StateError("checkpoint_exists", "本运行已有检查点；请显式恢复条件并重新规划。")
        state = self.graph.invoke({"run_id": self.run_id, "action": "middle",
                                   "completed_rounds": 0, "stop_reason": ""}, config=config)
        question = None
        if state["stop_reason"] == "needs_clarification":
            questions = [e["decision"]["question_code"] for e in self.trajectory
                         if e["step"] == "decision" and e["decision"]["action"] == "ask_user"]
            question = questions[-1] if questions else ("origin_a" if "a" not in self.tools.origins else "origin_b")
        self.store.record_planning_outcome(self.user_id, self.run_id, question)
        self.emit({"step": "stop", "reason": state["stop_reason"],
                   "completed_rounds": state["completed_rounds"]})
        return state["stop_reason"]


def inspect_checkpoint(store, user_id, run_id):
    run = store.get_run(user_id, run_id)  # 先按user+run验证归属，才读取检查点。
    path = checkpoint_path(store)
    if not path.is_file():
        return {"checkpoint_count": 0, "persistent_field_boundary_passed": None,
                "note": "尚未通过M5规划入口生成检查点。"}
    config = {"configurable": {"thread_id": thread_id(user_id, run["session_id"], run["constraints_version"], run_id)}}
    with saver_for(store) as saver:
        rows = list(saver.list(config))
        latest = saver.get_tuple(config)
        allowed = {"run_id", "action", "completed_rounds", "stop_reason"}
        # START写入也会进入checkpoint。既检查最终channel，也检查pending_writes。
        def safe_values(values):
            if not isinstance(values, dict):
                return False
            for key, value in values.items():
                if key == "__start__":
                    if not isinstance(value, dict) or not set(value) <= allowed:
                        return False
                    if any(not isinstance(v, (str, int)) for v in value.values()):
                        return False
                elif key in allowed:
                    if not isinstance(value, (str, int)):
                        return False
                elif key.startswith("branch:to:") or key == "__no_writes__":
                    if value is not None:
                        return False
                else:
                    return False
            return True
        forbidden = {"messages", "candidate", "candidates", "origins", "origin_text", "location", "routes",
                     "selected_route", "trajectory", "raw_response", "tool_results", "line_options", "poi_id", "address"}
        def clean_metadata(value):
            if isinstance(value, dict):
                return not (set(value) & forbidden) and all(clean_metadata(v) for v in value.values())
            if isinstance(value, (list, tuple)):
                return all(clean_metadata(v) for v in value)
            return value is None or isinstance(value, (str, int, float, bool))
        boundary = bool(rows) and all(safe_values(r.checkpoint["channel_values"])
                                      and clean_metadata(r.metadata) for r in rows)
        for row in rows:
            for _, channel, value in row.pending_writes or []:
                boundary = boundary and safe_values({channel: value})
        values = latest.checkpoint["channel_values"] if latest else {}
        return {"checkpoint_count": len(rows), "persistent_field_boundary_passed": bool(boundary),
                "latest_control_state": {k: values[k] for k in allowed if k in values},
                "resume_policy": "read_confirmed_conditions_then_requery"}