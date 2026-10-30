"""本地单进程网页适配层。候选、路线与点选列表只留在有期限的内存中。"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

from app.config import get_settings
from app.m1_models import StartRun
from app.m1_store import StateError
from app.m2 import run_meta
from app.m2_client import MapClient
from app.m2_models import MapToolError
from app.m2_tools import MapTools
from app.m3_baseline import error_reason, finish_planning
from app.m10_decider import M10LLMDecider as LLMDecider  # M10_INTEGRATION_V1
from app.m4_models import DecisionError
from app.m5_checkpoint import inspect_checkpoint, saver_for
from app.m10_planner import M10Planner as MemoryPlanner  # M10_INTEGRATION_V1
from app.m5_store import MemoryStore
from app.m9_evidence import EvidenceTools, web_view  # M9_INTEGRATION_V1

TERMINAL = {"completed", "failed", "cancelled", "superseded"}


class WebPlanner(MemoryPlanner):
    def guarded(self, function):
        guarded_node = super().guarded(function)

        def node(state):
            try:
                return guarded_node(state)
            except StateError as exc:
                # 取消/条件过期也以安全控制字段结束，避免异常对象进入checkpoint。
                self.error = {"code": exc.code, "message": exc.message}
                return {"stop_reason": "manual_stop"}
            except Exception:
                self.error = {"code": "web_node_error", "message": "执行节点未完成，本次安全结束。"}
                return {"stop_reason": "api_error"}

        return node


@contextmanager
def live_resources(store, request, run_id):
    client = decider = None
    try:
        settings = get_settings()
        client = MapClient(settings, store, request.user_id, run_id)
        decider = LLMDecider(settings, store, request.user_id, run_id)
        yield client, decider, MapTools(client, request.departure_date, request.departure_time)
    finally:
        if client is not None:
            client.close()
        if decider is not None:
            decider.close()


class WebJob:
    def __init__(self, store, task, run, request):
        self.store, self.task, self.run, self.request = store, task, run, request
        self.condition = threading.Condition()
        self.status, self.working, self.cancelled = "running", True, False
        self.pending, self.answer = None, None
        self.events, self.result, self.error, self.audit = [], None, None, None
        self.model, self.real_map_http_attempts, self.map_retries = None, 0, 0
        self.started, self.completed, self.elapsed = time.monotonic(), None, None
        self.confirmation_timed_out = False
        self.meta = run_meta()

    def event(self, event):
        fields = ("step", "action", "radius_meters", "round", "feedback", "remaining",
                  "decision", "reason", "decision_source", "source", "completed_rounds")
        with self.condition:
            self.events.append({k: deepcopy(event[k]) for k in fields if k in event})
            self.events = self.events[-80:]


class JobManager:
    def __init__(self, store_factory=MemoryStore, resources_factory=live_resources,
                 origin_timeout=180, retention_seconds=600):
        self.store_factory, self.resources_factory = store_factory, resources_factory
        self.origin_timeout, self.retention_seconds = origin_timeout, retention_seconds
        self.lock, self.jobs, self.active = threading.RLock(), {}, None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="meetpoint-web")

    def _expire(self):
        now = time.monotonic()
        for rid, job in list(self.jobs.items()):
            if not job.working and job.completed is not None and now - job.completed >= self.retention_seconds:
                del self.jobs[rid]

    def _get(self, user_id, run_id):
        with self.lock:
            self._expire()
            job = self.jobs.get(run_id)
            if job is None or job.task["user_id"] != user_id:
                raise StateError("web_run_not_found", "本用户下没有此网页运行，或临时结果已过期；请恢复条件后重新规划。", 404)
            return job

    def start(self, session_id, request):
        with self.lock:
            self._expire()
            if self.active in self.jobs and self.jobs[self.active].working:
                raise StateError("web_busy", "已有网页规划正在运行或等待确认；先完成或停止它。")
            store = self.store_factory()
            memory = store.get_memory(request.user_id, session_id)
            task = memory["task"]
            if memory["pending_question_code"] == "explicit_change":
                raise StateError("unresolved_change", "上一条修改尚不明确，请先提交明确的新值。")
            run = store.start_run(session_id, StartRun(user_id=request.user_id,
                expected_version=request.expected_version), limits=request.limits, execution_mode="m6_live")
            job = WebJob(store, task, run, request)
            finished = [rid for rid, old in self.jobs.items() if not old.working]
            for rid in finished[:max(0, len(self.jobs) - 9)]:
                del self.jobs[rid]
            self.jobs[run["run_id"]], self.active = job, run["run_id"]
            self.pool.submit(self._work, job)
            return {"run_id": run["run_id"], "session_id": session_id,
                    "constraints_version": run["constraints_version"], "status": "running"}

    def _choose_origin(self, job, role, query, result):
        with job.condition:
            job.store.assert_run_current(job.request.user_id, job.run["run_id"])
            job.answer = None
            job.pending = {"prompt_id": uuid4().hex, "role": role, "query": query,
                           "places": [p.model_dump() for p in result.places],
                           "timeout_seconds": self.origin_timeout}
            job.status = "awaiting_origin"
            deadline = time.monotonic() + self.origin_timeout
            while job.answer is None:
                if job.cancelled:
                    raise StateError("web_cancelled", "本次网页规划已停止。")
                job.store.assert_run_current(job.request.user_id, job.run["run_id"])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    job.confirmation_timed_out = True
                    job.pending, job.status = None, "running"
                    return None
                job.condition.wait(min(1, remaining))
            answer = job.answer
            job.pending, job.answer, job.status = None, None, "running"
            return answer

    def choose(self, run_id, request):
        job = self._get(request.user_id, run_id)
        with job.condition:
            pending = job.pending
            if (job.cancelled or job.status != "awaiting_origin" or pending is None
                    or request.prompt_id != pending["prompt_id"] or job.answer is not None):
                raise StateError("stale_choice", "这份确认列表已失效，请刷新当前运行。")
            job.store.assert_run_current(request.user_id, run_id)
            if request.index > len(pending["places"]):
                raise StateError("invalid_choice", "只能选择本次返回列表中的序号。", 422)
            job.answer = request.index
            job.condition.notify_all()
            return {"accepted": True}

    def cancel(self, user_id, run_id):
        job = self._get(user_id, run_id)
        with job.condition:
            if not job.working:
                return {"accepted": False, "status": job.status}
            job.cancelled, job.pending = True, None
            current = job.store.get_run(user_id, run_id)
            if current["status"] == "running":
                job.store.finish_run(user_id, run_id, "manual_stop")
            job.condition.notify_all()
            return {"accepted": True, "status": "stopping",
                    "note": "已发出的HTTP请求会等待返回或超时，随后不再发起新请求。"}

    def snapshot(self, user_id, run_id):
        job = self._get(user_id, run_id)
        with job.condition:
            run = job.store.get_run(user_id, run_id)
            task = job.store.get_task(user_id, job.task["session_id"])
            stale = (task["constraints_version"] != run["constraints_version"] or run["status"] == "superseded")
            status = "superseded" if stale else job.status
            if job.cancelled and job.working and not stale:
                status = "stopping"
            return deepcopy({
                "run_id": run_id, "status": status, "working": job.working,
                "session_id": job.task["session_id"], "constraints_version": run["constraints_version"],
                "departure_date": job.request.departure_date, "departure_time": job.request.departure_time,
                "run": run, "pending": None if stale or job.cancelled else job.pending,
                "result": None if stale or job.cancelled else job.result,
                "events": job.events, "error": job.error, "model": job.model,
                "real_map_http_attempts": job.real_map_http_attempts, "map_retries": job.map_retries,
                "elapsed_seconds": job.elapsed if job.elapsed is not None else round(time.monotonic()-job.started, 3),
                "checkpoint_audit": job.audit, "confirmation_timed_out": job.confirmation_timed_out,
                "meta": job.meta, "result_ttl_seconds": self.retention_seconds,
                "resume_policy": "read_confirmed_conditions_then_requery"})

    def _work(self, job):
        store, user_id, rid = job.store, job.request.user_id, job.run["run_id"]
        reason, error, planner, model, attempts, retries = "api_error", None, None, None, 0, 0
        try:
            with self.resources_factory(store, job.request, rid) as (client, decider, tools):
                observer = EvidenceTools(tools, rid, job.run["constraints_version"])
                try:
                    store.assert_run_current(user_id, rid)
                    with saver_for(store) as saver:
                        planner = WebPlanner(observer, job.task, decider, saver,
                            choose_origin=lambda *args: self._choose_origin(job, *args), on_event=job.event)
                        reason, error = planner.run(), planner.error
                finally:
                    model = decider.metadata()
                    attempts = getattr(client, "attempts_started", 0)
                    retries = getattr(client, "retries_started", 0)
        except StateError as exc:
            reason, error = error_reason(exc), {"code": exc.code, "message": exc.message}
        except MapToolError as exc:
            reason, error = error_reason(exc), {"code": exc.code, "message": exc.message}
        except DecisionError as exc:
            reason, error = "llm_error", {"code": exc.code, "message": exc.message}
        except Exception:
            error = {"code": "web_internal_error", "message": "网页规划异常结束，请先跑离线回归并核对配置。"}
        result, audit, status = None, None, "completed"
        try:
            if job.cancelled:
                current = store.get_run(user_id, rid)
                if current["status"] == "running":
                    store.finish_run(user_id, rid, "manual_stop")
                status, error = "cancelled", None
            else:
                finished = finish_planning(store, user_id, rid, reason)
                if planner is not None:
                    result = planner.summary(finished["stop_reason"])
                    result["m9_presentation"] = web_view(observer, result, job.task["requirements"], finished["stop_reason"])
                    if not result["m9_presentation"]["evidence_valid"]:
                        result["recommendations"] = []
                        status, error = "failed", {"code": "evidence_validation_failed", "message": "推荐证据未通过校验。"}
                if error:
                    status = "failed"
            audit = inspect_checkpoint(store, user_id, rid)
            if audit["persistent_field_boundary_passed"] is False:
                status, result = "failed", None
                error = {"code": "checkpoint_boundary_failed", "message": "检查点字段边界未通过校验，请核对当前代码。"}
        except StateError as exc:
            status = "superseded" if exc.code in ("stale_run", "version_conflict") else "failed"
            result, error = None, {"code": exc.code, "message": exc.message}
        except Exception:
            status, result = "failed", None
            error = {"code": "web_finalize_error", "message": "本次运行结束记录未通过校验，请恢复条件后重试。"}
        finally:
            with job.condition:
                job.status, job.result, job.error, job.audit = status, result, error, audit
                job.model, job.real_map_http_attempts, job.map_retries = model, attempts, retries
                job.pending, job.answer, job.working = None, None, False
                job.completed, job.elapsed = time.monotonic(), round(time.monotonic() - job.started, 3)
                job.condition.notify_all()
            timer = threading.Timer(self.retention_seconds, self._cleanup)
            timer.daemon = True
            timer.start()

    def _cleanup(self):
        with self.lock:
            self._expire()

    def close(self):
        with self.lock:
            jobs = list(self.jobs.items())
        for rid, job in jobs:
            if job.working:
                self.cancel(job.request.user_id, rid)
        self.pool.shutdown(wait=True, cancel_futures=True)