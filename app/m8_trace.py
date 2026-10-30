"""M8 调用观测。仅自建环境保存证据；不接受真实地图原始响应。"""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


INPUT_CAP = 32768
OUTPUT_CAP = 512
INPUT_PRICE = 0.2
OUTPUT_PRICE = 0.8
CALL_RESERVE_YUAN = (INPUT_CAP * INPUT_PRICE + OUTPUT_CAP * OUTPUT_PRICE) / 1_000_000


class BudgetStop(RuntimeError):
    pass


def append_json(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return [], []
    rows, errors = [], []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not_object")
            rows.append(row)
        except (ValueError, TypeError):
            errors.append({"file": path.name, "line": number, "error": "invalid_json"})
    return rows, errors


@contextmanager
def exclusive_run(root):
    """跨批次串行计费；进程退出自动释放操作系统锁。"""
    path = Path(root) / "m8.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            f.write(b"0")
            f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def valid_usage(value):
    return (isinstance(value, dict)
            and all(type(value.get(k)) is int and value[k] >= 0
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
            and value["total_tokens"] == value["prompt_tokens"] + value["completion_tokens"])


def cost_yuan(usage):
    """北京地域普通实时原价估算；缓存细项不重复加，非账单扣费。"""
    if not valid_usage(usage):
        return None
    p = usage["prompt_tokens"]
    multiplier = 1 if p <= 32768 else 3 if p <= 262144 else 6
    return (p * INPUT_PRICE + usage["completion_tokens"] * OUTPUT_PRICE) * multiplier / 1e6


class SpendLedger:
    def __init__(self, root, batch_id, batch_limit=1.0, total_limit=5.0):
        self.path = Path(root) / "spend.jsonl"
        self.batch_id, self.batch_limit, self.total_limit = batch_id, batch_limit, total_limit

    def spent(self, batch_only=False):
        rows, errors = read_jsonl(self.path)
        if errors:
            raise BudgetStop("spend_ledger_corrupt")
        reserved, settled = {}, {}
        for row in rows:
            if batch_only and row["batch_id"] != self.batch_id:
                continue
            if row["event"] == "reserve":
                if row["attempt_id"] in reserved:
                    raise BudgetStop("duplicate_spend_reservation")
                reserved[row["attempt_id"]] = row["yuan"]
            elif row["event"] == "settle":
                settled[row["attempt_id"]] = row["yuan"]
        return sum(settled.get(a, amount) for a, amount in reserved.items())

    def reserve(self, attempt_id):
        if self.spent() + CALL_RESERVE_YUAN > self.total_limit + 1e-12:
            raise BudgetStop("project_money_limit")
        if self.spent(True) + CALL_RESERVE_YUAN > self.batch_limit + 1e-12:
            raise BudgetStop("batch_money_limit")
        append_json(self.path, {"event": "reserve", "attempt_id": attempt_id,
                    "batch_id": self.batch_id, "yuan": CALL_RESERVE_YUAN})

    def settle(self, attempt_id, usage):
        amount = cost_yuan(usage)
        if amount is not None:
            append_json(self.path, {"event": "settle", "attempt_id": attempt_id,
                        "batch_id": self.batch_id, "yuan": amount})


class Trace:
    def __init__(self, trial_id, run_id, sink=None):
        self.trial_id, self.run_id, self.sink = trial_id, run_id, sink
        self.llm, self.tools, self.http, self.evidence, self.rejected = [], [], [], [], []
        self.complete = True

    def emit(self, event, **data):
        row = {"event": event, "trial_id": self.trial_id, "run_id": self.run_id, **data}
        if self.sink:
            self.sink(row)

    def export(self):
        return {"complete": self.complete, "llm": self.llm, "tools": self.tools,
                "simulated_http": self.http, "real_map_http": 0,
                "evidence": self.evidence, "rejected_actions": self.rejected}


class AuditedClient:
    """在SDK调用边界观测每次真实请求；SDK重试必须关闭。"""
    def __init__(self, delegate, trace, ledger=None):
        self.delegate, self.trace, self.ledger = delegate, trace, ledger
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        # UTF-8字节数加保守协议余量，用于调用前控制；报告只使用服务端usage。
        bound = len(json.dumps(kwargs, ensure_ascii=False).encode("utf-8")) + 4096
        if bound > INPUT_CAP or kwargs.get("max_tokens", OUTPUT_CAP) > OUTPUT_CAP:
            raise BudgetStop("request_reservation_bound_exceeded")
        aid = uuid4().hex
        if self.ledger:
            self.ledger.reserve(aid)
        row = {"attempt_id": aid, "purpose": "search_decision", "usage": None,
               "returned_model": None, "status": "started", "error": None}
        self.trace.llm.append(row)
        self.trace.emit("llm_start", **row)
        started = time.perf_counter()
        try:
            response = self.delegate.chat.completions.create(**kwargs)
            usage = getattr(response, "usage", None)
            row["usage"] = (usage.model_dump(exclude_none=True)
                            if hasattr(usage, "model_dump") else usage)
            row["returned_model"] = getattr(response, "model", None)
            row["status"] = "returned"
            if self.ledger:
                self.ledger.settle(aid, row["usage"])
            if valid_usage(row["usage"]) and (row["usage"]["prompt_tokens"] > INPUT_CAP
                                              or row["usage"]["completion_tokens"] > OUTPUT_CAP):
                self.trace.complete = False
                raise BudgetStop("provider_usage_exceeds_reservation")
            return response
        except Exception as exc:
            row["status"], row["error"] = "error", type(exc).__name__
            row["http_status"] = getattr(exc, "status_code", None)
            raise
        finally:
            row["elapsed_seconds"] = round(time.perf_counter() - started, 6)
            self.trace.emit("llm_end", **row)

    def close(self):
        self.delegate.close()
