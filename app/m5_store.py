"""扩展原M1数据库；用户条件、偏好、来源与变更事件均在事务内写入。"""

import json
from uuid import uuid4

from app.m1_models import Requirements, RequirementsPatch, UpdateTask, merge_requirements
from app.m1_store import StateError, StateStore, now
from app.m5_models import (
    DeletePreferences, MemoryMessage, MemoryPatch, NewMemoryTask,
    RecoverRequest, SetPreferences,
)
from app.m5_parse import parse_message


def flatten(data, prefix=""):
    result = {}
    for key, value in data.items():
        name = prefix + key
        if isinstance(value, dict):
            result.update(flatten(value, name + "."))
        else:
            result[name] = value
    return result


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class MemoryStore(StateStore):
    def __init__(self, path=None):
        super().__init__(path)
        with self.write() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL CHECK(revision>=1),
                values_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS memory_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_id TEXT REFERENCES task_sessions(session_id),
                constraints_version INTEGER,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS memory_session_idx ON memory_events(user_id,session_id,event_id)")
        # 不提升M1 PRAGMA user_version；原StateStore仍可访问同一数据库。

    @staticmethod
    def _prefs(conn, user_id):
        row = conn.execute("SELECT * FROM user_preferences WHERE user_id=?", (user_id,)).fetchone()
        return {"revision": row["revision"] if row else 0,
                "values": json.loads(row["values_json"]) if row else {},
                "scope": "user_A_defaults"}

    @staticmethod
    def _event(conn, user_id, session_id, version, kind, payload):
        conn.execute("""INSERT INTO memory_events
            (user_id,session_id,constraints_version,kind,payload_json,created_at)
            VALUES (?,?,?,?,?,?)""", (user_id, session_id, version, kind, encoded(payload), now()))

    @staticmethod
    def _revision(preferences, expected):
        if expected != preferences["revision"]:
            raise StateError("preference_version_conflict", "偏好已变化，请重新读取revision。",
                             actual_revision=preferences["revision"], expected_revision=expected)

    def _memory(self, conn, user_id, session_id):
        task = self.task_data(self._task(conn, user_id, session_id))
        current = flatten(task["requirements"])
        sources = {field: "existing_confirmed" for field in current}
        pending, pending_version, tracked_version = None, None, None
        for row in conn.execute("""SELECT * FROM memory_events
                WHERE user_id=? AND session_id=? ORDER BY event_id""", (user_id, session_id)):
            payload = json.loads(row["payload_json"])
            if row["kind"] == "task_created":
                sources.update(payload["field_sources"])
                tracked_version = row["constraints_version"]
                pending = None
            elif row["kind"] in ("conditions_changed", "conditions_unchanged"):
                if tracked_version is not None and tracked_version != payload["base_version"]:
                    sources = {field: "existing_confirmed" for field in current}
                for field in payload.get("confirmed_fields", payload["changed_values"]):
                    sources[field] = payload["source"]
                tracked_version, pending = row["constraints_version"], None
            elif row["kind"] == "clarification_needed":
                pending, pending_version = payload["question_code"], row["constraints_version"]
            elif row["kind"] == "clarification_cleared":
                pending = None
        if tracked_version != task["constraints_version"]:
            # 其他M1接口修改过条件：没有证据就不能冒称其详细来源。
            sources = {field: "existing_confirmed" for field in current}
        if pending_version != task["constraints_version"]:
            pending = None
        return {"task": task, "field_sources": sources,
                "pending_question_code": pending, "preferences": self._prefs(conn, user_id)}

    def get_memory(self, user_id, session_id):
        with self.connection() as conn:
            return self._memory(conn, user_id, session_id)

    def read_preferences(self, user_id):
        with self.connection() as conn:
            return self._prefs(conn, user_id)

    def create_memory_task(self, request):
        request = NewMemoryTask.model_validate(request)
        sid, timestamp = uuid4().hex, now()
        with self.write() as conn:
            preferences = self._prefs(conn, request.user_id)
            requirements = Requirements.model_validate({
                "person_a": {"origin_text": request.person_a.origin_text},
                "person_b": {"origin_text": request.person_b.origin_text},
            }).model_dump()
            sources = {field: "default" for field in flatten(requirements)}
            sources.update({"person_a.origin_text": "current_explicit", "person_b.origin_text": "current_explicit"})
            for key, value in preferences["values"].items():
                if key == "preference":
                    requirements[key] = value
                    sources[key] = "long_term_explicit"
                else:
                    requirements["person_a"][key] = value
                    sources["person_a." + key] = "long_term_explicit"
            explicit = {"person_a": request.person_a.model_dump(exclude_unset=True),
                        "person_b": request.person_b.model_dump(exclude_unset=True)}
            if request.preference is not None:
                explicit["preference"] = request.preference
            for field in flatten(explicit):
                sources[field] = "current_explicit"
            final = merge_requirements(Requirements.model_validate(requirements), RequirementsPatch.model_validate(explicit))
            conn.execute("""INSERT INTO task_sessions
                (session_id,user_id,constraints_version,requirements_json,phase,active_run_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""", (sid, request.user_id, 1, final.model_dump_json(), "ready", None, timestamp, timestamp))
            self._event(conn, request.user_id, sid, 1, "task_created", {
                "field_sources": sources, "preferences_revision": preferences["revision"],
            })
            return self._memory(conn, request.user_id, sid)

    def _patch(self, conn, session_id, request, source):
        row = self._task(conn, request.user_id, session_id)
        self.check_version(row, request.expected_version)
        current = Requirements.model_validate_json(row["requirements_json"])
        final = merge_requirements(current, request.changes)
        before, after = flatten(current.model_dump()), flatten(final.model_dump())
        changed = {key: value for key, value in after.items() if before[key] != value}
        version = row["constraints_version"]
        if changed:
            timestamp = now()
            if row["active_run_id"] is not None:
                conn.execute("""UPDATE run_metrics SET status='superseded',stop_reason='conditions_changed',updated_at=?
                    WHERE run_id=? AND status='running'""", (timestamp, row["active_run_id"]))
            result = conn.execute("""UPDATE task_sessions SET requirements_json=?,constraints_version=constraints_version+1,
                phase='ready',active_run_id=NULL,updated_at=?
                WHERE user_id=? AND session_id=? AND constraints_version=?""",
                (final.model_dump_json(), timestamp, request.user_id, session_id, version))
            if result.rowcount != 1:
                raise StateError("version_conflict", "条件版本冲突，操作已回滚。")
            version += 1
        self._event(conn, request.user_id, session_id, version,
                    "conditions_changed" if changed else "conditions_unchanged",
                    {"changed_values": changed, "source": source, "base_version": row["constraints_version"],
                     "confirmed_fields": sorted(flatten(request.changes.model_dump(exclude_unset=True)))})
        return {"status": "updated" if changed else "unchanged", "changed_fields": sorted(changed),
                "replan_required": bool(changed), **self._memory(conn, request.user_id, session_id)}

    def patch_memory_task(self, session_id, request):
        request = MemoryPatch.model_validate(request)
        with self.write() as conn:
            return self._patch(conn, session_id, request, "structured_explicit")

    def _write_preferences(self, conn, user_id, expected_revision, changes, delete_keys=None, session_id=None):
        before = self._prefs(conn, user_id)
        self._revision(before, expected_revision)
        final = dict(before["values"])
        if delete_keys is not None:
            for key in delete_keys or list(final):
                final.pop(key, None)
        else:
            final.update(changes)
        if final != before["values"]:
            revision = before["revision"] + 1
            conn.execute("""INSERT INTO user_preferences VALUES (?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET revision=excluded.revision,
                values_json=excluded.values_json,updated_at=excluded.updated_at""", (user_id, revision, encoded(final), now()))
            self._event(conn, user_id, session_id, None, "preferences_changed", {
                "revision": revision, "source": "long_term_explicit",
                "changed_values": {k: v for k, v in final.items() if before["values"].get(k) != v},
                "deleted_keys": sorted(set(before["values"]) - set(final)),
            })
        return {"changed": final != before["values"], **self._prefs(conn, user_id)}

    def save_preferences(self, request):
        request = SetPreferences.model_validate(request)
        with self.write() as conn:
            return self._write_preferences(conn, request.user_id, request.expected_revision,
                                           request.values.model_dump(exclude_unset=True))

    def delete_preferences(self, request):
        request = DeletePreferences.model_validate(request)
        with self.write() as conn:
            return self._write_preferences(conn, request.user_id, request.expected_revision, {}, request.keys)

    def apply_message(self, session_id, request):
        request = MemoryMessage.model_validate(request)
        # 整句解析；成功之后才开启事务，没有正则命中就部分写入的路径。
        proposal = parse_message(request.text)
        with self.write() as conn:
            row = self._task(conn, request.user_id, session_id)
            self.check_version(row, request.expected_version)
            if proposal["intent"] == "clarify":
                self._event(conn, request.user_id, session_id, row["constraints_version"],
                            "clarification_needed", {"question_code": proposal["question_code"]})
                return {"status": "needs_clarification", "question": proposal["question"],
                        "changed_fields": [], "replan_required": False,
                        **self._memory(conn, request.user_id, session_id)}
            if proposal["intent"] == "current":
                patch = UpdateTask(user_id=request.user_id, expected_version=request.expected_version,
                                   changes=RequirementsPatch.model_validate(proposal["changes"]))
                return self._patch(conn, session_id, patch, "user_explicit_sentence")
            if request.expected_preferences_revision is None:
                raise StateError("preference_revision_required", "先读取偏好revision，再提交长期偏好修改。", 422)
            preferences = self._write_preferences(conn, request.user_id, request.expected_preferences_revision,
                proposal.get("values", {}), [] if proposal["intent"] == "delete_preferences" else None, session_id)
            return {"status": "preferences_updated" if preferences["changed"] else "unchanged",
                    "replan_required": False, "changed_fields": [],
                    **self._memory(conn, request.user_id, session_id)}

    def recover_task(self, session_id, request):
        request = RecoverRequest.model_validate(request)
        with self.write() as conn:
            row = self._task(conn, request.user_id, session_id)
            self.check_version(row, request.expected_version)
            active = row["active_run_id"]
            if active:
                conn.execute("""UPDATE run_metrics SET status='stopped',stop_reason='manual_stop',updated_at=?
                    WHERE run_id=? AND status='running'""", (now(), active))
                conn.execute("UPDATE task_sessions SET phase='ready',active_run_id=NULL,updated_at=? WHERE session_id=?",
                             (now(), session_id))
                self._event(conn, request.user_id, session_id, row["constraints_version"], "recovery_requested", {})
            return {"interrupted_run_stopped": bool(active), "requery_routes_required": True,
                    **self._memory(conn, request.user_id, session_id)}

    def record_planning_outcome(self, user_id, run_id, question_code):
        if question_code not in (None, "origin_a", "origin_b", "duration", "transfers", "walking", "direction"):
            raise StateError("invalid_question_code", "不能保存未验证的问题内容。", 422)
        with self.write() as conn:
            run = self._run(conn, user_id, run_id)
            task = self._task(conn, user_id, run["session_id"])
            if (run["constraints_version"] == task["constraints_version"]
                    and run["status"] == "stopped"
                    and str(run["stop_reason"]).startswith("budget_exhausted:")
                    and task["active_run_id"] is None):
                pass  # reserve已原子记录预算停止；仅保存控制事件，不恢复运行。
            else:
                self.check_current(task, run)
            self._event(conn, user_id, run["session_id"], run["constraints_version"],
                "clarification_needed" if question_code else "clarification_cleared",
                {"question_code": question_code} if question_code else {})

    def events(self, user_id, session_id):
        with self.connection() as conn:
            self._task(conn, user_id, session_id)
            rows = conn.execute("""SELECT event_id,kind,constraints_version,payload_json,created_at
                FROM memory_events WHERE user_id=? AND session_id=? ORDER BY event_id DESC LIMIT 100""", (user_id, session_id))
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                result.append(item)
            return result