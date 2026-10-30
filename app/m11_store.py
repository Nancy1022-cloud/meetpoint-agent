"""Reviewable edits; confirm once with two optimistic version checks in one transaction."""
import json
import time
from uuid import uuid4
from app.m1_models import RequirementsPatch
from app.m1_store import StateError, now
from app.m5_models import MemoryPatch
from app.m5_store import MemoryStore, encoded, flatten
from app.m11_models import PreviewRequest, ConfirmRequest, QUESTIONS, PROTOCOL
from app.m11_parse import changes
from app.m11_scope import scope_decision

class ProposalStore(MemoryStore):
    def __init__(self, path=None, *, clock=time.time, ttl=600):
        super().__init__(path)
        self.clock, self.ttl = clock, ttl
        with self.write() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS m11_proposals (
                proposal_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                base_version INTEGER NOT NULL, base_revision INTEGER NOT NULL,
                expires_at REAL NOT NULL, status TEXT NOT NULL,
                changes_json TEXT NOT NULL, receipt_json TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES task_sessions(session_id))""")
            conn.execute("CREATE INDEX IF NOT EXISTS m11_owner ON m11_proposals(user_id,session_id,created_at)")

    def _check(self, conn, sid, request):
        task = self._task(conn, request.user_id, sid)
        self.check_version(task, request.expected_version)
        self._revision(self._prefs(conn, request.user_id), request.expected_preferences_revision)
        return task

    @staticmethod
    def _diff(memory, current, defaults):
        before = flatten(memory["task"]["requirements"])
        return [{"scope":"current", "path":k, "before":before[k], "after":v}
                for k,v in flatten(current).items()] + [
            {"scope":"defaults", "path":k, "before":memory["preferences"]["values"].get(k), "after":v}
            for k,v in defaults.items()]

    def preview(self, session_id, request, parser):
        request = PreviewRequest.model_validate(request)
        # No transaction stays open while waiting on the model.
        with self.connection() as conn:
            self._check(conn, session_id, request)
            memory = self._memory(conn, request.user_id, session_id)
        decision = scope_decision(request.text)
        if decision["decision"] == "refuse":
            with self.write() as conn:
                self._check(conn, session_id, request)
                self._event(conn, request.user_id, session_id, request.expected_version,
                    "m11_refused", {"reason":decision["scope_reason"], "protocol":PROTOCOL})
            return {"status":"refused", "message":decision["message"], "scope_reason":decision["scope_reason"],
                    "changed_fields":[], "replan_required":False, "protocol":PROTOCOL}
        proposal = parser(request.text, memory)
        with self.write() as conn:
            self._check(conn, session_id, request)
            if proposal.intent == "clarify":
                self._event(conn, request.user_id, session_id, request.expected_version,
                    "m11_clarification", {"question_code":proposal.question_code, "parser":request.parser})
                return {"status":"needs_clarification", "question":QUESTIONS[proposal.question_code],
                    "question_code":proposal.question_code, "changed_fields":[], "replan_required":False,
                    "protocol":PROTOCOL}
            current, defaults = changes(proposal)
            data = {"current":current, "defaults":defaults, "parser":request.parser,
                    "diff":self._diff(memory,current,defaults)}
            pid, expiry = uuid4().hex, self.clock()+self.ttl
            conn.execute("INSERT INTO m11_proposals VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid,request.user_id,session_id,request.expected_version,request.expected_preferences_revision,
                 expiry,"pending",encoded(data),None,now()))
            self._event(conn,request.user_id,session_id,request.expected_version,"m11_proposed",
                {"proposal_id":pid,"parser":request.parser,"fields":[d["scope"]+":"+d["path"] for d in data["diff"]]})
            return {"status":"awaiting_confirmation","proposal_id":pid,"base_version":request.expected_version,
                "base_revision":request.expected_preferences_revision,"expires_at":expiry,"diff":data["diff"],
                "changed_fields":[],"replan_required":False,"protocol":PROTOCOL,
                "notice":"请核对人物、字段与范围。确认前未修改条件；默认值仅用于之后新建的会话。"}

    def _proposal(self, conn, user_id, sid, pid):
        self._task(conn,user_id,sid)
        row=conn.execute("SELECT * FROM m11_proposals WHERE proposal_id=? AND user_id=? AND session_id=?",
                         (pid,user_id,sid)).fetchone()
        if row is None: raise StateError("proposal_not_found","本会话下没有这份修改预览。",404)
        return row

    def confirm(self, session_id, proposal_id, request):
        request=ConfirmRequest.model_validate(request)
        with self.write() as conn:
            row=self._proposal(conn,request.user_id,session_id,proposal_id)
            if (request.expected_version!=row["base_version"] or
                    request.expected_preferences_revision!=row["base_revision"]):
                raise StateError("proposal_request_mismatch","提交的版本与预览不一致，请重新生成预览。")
            if row["status"]=="applied":
                # An identical transport retry is not another write; current Memory
                # is returned separately from the original application receipt.
                receipt=json.loads(row["receipt_json"])
                return {"status":"already_applied", "receipt":receipt,
                    "replan_required":receipt["current_changed"], **self._memory(conn,request.user_id,session_id)}
            if row["status"]!="pending":
                raise StateError("proposal_closed","此预览已取消，不能再次确认。")
            if self.clock()>=row["expires_at"]:
                raise StateError("proposal_expired","预览已过期，请根据当前条件重新生成。")
            self._check(conn,session_id,request)
            data=json.loads(row["changes_json"])
            current,defaults=data["current"],data["defaults"]
            before=self._memory(conn,request.user_id,session_id)
            if defaults:
                self._write_preferences(conn,request.user_id,request.expected_preferences_revision,
                                        defaults,session_id=session_id)
            result=None
            if current:
                result=self._patch(conn,session_id,MemoryPatch(user_id=request.user_id,
                    expected_version=request.expected_version, changes=RequirementsPatch.model_validate(current)),
                    "m11_user_confirmed_"+data["parser"])
            memory=self._memory(conn,request.user_id,session_id)
            receipt={"proposal_id":proposal_id,"constraints_version":memory["task"]["constraints_version"],
                "preferences_revision":memory["preferences"]["revision"],"diff":data["diff"],
                "current_changed":bool(result and result["changed_fields"]),
                "defaults_changed":before["preferences"]["values"]!=memory["preferences"]["values"],
                "source":"user_confirmed_"+data["parser"]}
            conn.execute("UPDATE m11_proposals SET status='applied',receipt_json=? WHERE proposal_id=?",
                         (encoded(receipt),proposal_id))
            self._event(conn,request.user_id,session_id,memory["task"]["constraints_version"],"m11_confirmed",receipt)
            return {"status":"applied","receipt":receipt,"replan_required":receipt["current_changed"],**memory}

    def cancel(self, session_id, proposal_id, user_id):
        with self.write() as conn:
            row=self._proposal(conn,user_id,session_id,proposal_id)
            if row["status"]=="applied": raise StateError("proposal_already_applied","已应用的预览不能取消。默认值可单独删除。")
            conn.execute("UPDATE m11_proposals SET status='cancelled' WHERE proposal_id=?",(proposal_id,))
            return {"status":"cancelled","changed_fields":[],"replan_required":False}

    def history(self,user_id,session_id):
        with self.connection() as conn:
            self._task(conn,user_id,session_id)
            rows=conn.execute("SELECT * FROM m11_proposals WHERE user_id=? AND session_id=? ORDER BY created_at DESC LIMIT 50",
                              (user_id,session_id)).fetchall()
            return [{"proposal_id":r["proposal_id"],"status":r["status"],"base_version":r["base_version"],
                     "base_revision":r["base_revision"],"expires_at":r["expires_at"],
                     "diff":json.loads(r["changes_json"])["diff"]} for r in rows]
