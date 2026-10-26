import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.m1_models import (
    BudgetCounters, BudgetLimits, CreateTask, Requirements,
    Reservation, StartRun, UpdateTask, merge_requirements,
)


class StateError(Exception):
    def __init__(self, code, message, status=409, **details):
        self.code = code
        self.message = message
        self.status = status
        self.details = details
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class StateStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get(
            "MEETPOINT_DB_PATH", "data/meetpoint.sqlite3"
        )).resolve()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def connection(self):
        # 每次操作独立连接，不共享跨线程的 Connection。
        conn = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")

        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")

            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def init_db(self):
        with self.write() as conn:
            schema_version = conn.execute(
                "PRAGMA user_version"
            ).fetchone()[0]

            if schema_version not in (0, 1):
                raise StateError(
                    "schema_mismatch",
                    "数据库版本不兼容。",
                    503,
                )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    constraints_version INTEGER NOT NULL
                        CHECK(constraints_version>=1),
                    requirements_json TEXT NOT NULL,
                    phase TEXT NOT NULL
                        CHECK(phase IN ('ready','running','stopped')),
                    active_run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_metrics (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL
                        REFERENCES task_sessions(session_id),
                    constraints_version INTEGER NOT NULL,
                    execution_mode TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN (
                            'running','finished','stopped','superseded'
                        )),
                    limits_json TEXT NOT NULL,
                    counters_json TEXT NOT NULL,
                    stop_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS task_user_idx
                ON task_sessions(user_id)
            """)

            conn.execute("PRAGMA user_version=1")

    @staticmethod
    def task_data(row):
        data = dict(row)

        data["requirements"] = Requirements.model_validate_json(
            data.pop("requirements_json")
        ).model_dump()

        return data

    @staticmethod
    def run_data(row):
        data = dict(row)

        limits = BudgetLimits.model_validate_json(
            data.pop("limits_json")
        )
        counts = BudgetCounters.model_validate_json(
            data.pop("counters_json")
        )

        data["limits"] = limits.model_dump()
        data["counters"] = counts.model_dump()

        data["remaining"] = {
            "map_http": limits.max_map_http - counts.map_http,
            "llm_calls": limits.max_llm_calls - counts.llm_calls,
            "search_rounds": (
                limits.max_search_rounds - counts.search_rounds
            ),
            "candidates_in_round": (
                limits.max_candidates_per_round
                - counts.candidates_in_round
            ),
        }

        return data

    def _task(self, conn, user_id, session_id):
        row = conn.execute(
            """
            SELECT * FROM task_sessions
            WHERE user_id=? AND session_id=?
            """,
            (user_id, session_id),
        ).fetchone()

        if row is None:
            raise StateError(
                "task_not_found",
                "该演示用户下没有此任务。",
                404,
            )

        return row

    def _run(self, conn, user_id, run_id):
        row = conn.execute(
            """
            SELECT r.*
            FROM run_metrics r
            JOIN task_sessions t ON t.session_id=r.session_id
            WHERE t.user_id=? AND r.run_id=?
            """,
            (user_id, run_id),
        ).fetchone()

        if row is None:
            raise StateError(
                "run_not_found",
                "该演示用户下没有此运行。",
                404,
            )

        return row

    @staticmethod
    def check_version(row, expected_version):
        if row["constraints_version"] != expected_version:
            raise StateError(
                "version_conflict",
                "条件已更新，请重新读取后再提交。",
                expected_version=expected_version,
                actual_version=row["constraints_version"],
            )

    @staticmethod
    def check_current(task, run):
        if run["constraints_version"] != task["constraints_version"]:
            raise StateError(
                "stale_run",
                "此运行使用旧条件，禁止继续执行或发布结果。",
            )

        if (
            run["status"] != "running"
            or task["active_run_id"] != run["run_id"]
        ):
            raise StateError(
                "run_not_running",
                "此运行已结束，不能继续扣预算。",
            )

    def create_task(self, request: CreateTask):
        request = CreateTask.model_validate(request)
        sid, timestamp = uuid4().hex, now()

        with self.write() as conn:
            conn.execute(
                "INSERT INTO task_sessions VALUES (?,?,?,?,?,?,?,?)",
                (
                    sid,
                    request.user_id,
                    1,
                    request.requirements.model_dump_json(),
                    "ready",
                    None,
                    timestamp,
                    timestamp,
                ),
            )

            return self.task_data(
                self._task(conn, request.user_id, sid)
            )

    def get_task(self, user_id, session_id):
        with self.connection() as conn:
            return self.task_data(
                self._task(conn, user_id, session_id)
            )

    def update_task(self, session_id, request: UpdateTask):
        request = UpdateTask.model_validate(request)

        with self.write() as conn:
            row = self._task(conn, request.user_id, session_id)
            self.check_version(row, request.expected_version)

            current = Requirements.model_validate_json(
                row["requirements_json"]
            )
            updated = merge_requirements(current, request.changes)

            if updated == current:
                return {
                    "changed": False,
                    "task": self.task_data(row),
                }

            timestamp = now()

            if row["active_run_id"] is not None:
                conn.execute(
                    """
                    UPDATE run_metrics
                    SET status='superseded',
                        stop_reason='conditions_changed',
                        updated_at=?
                    WHERE run_id=? AND status='running'
                    """,
                    (timestamp, row["active_run_id"]),
                )

            result = conn.execute(
                """
                UPDATE task_sessions
                SET requirements_json=?,
                    constraints_version=constraints_version+1,
                    phase='ready',
                    active_run_id=NULL,
                    updated_at=?
                WHERE user_id=?
                    AND session_id=?
                    AND constraints_version=?
                """,
                (
                    updated.model_dump_json(),
                    timestamp,
                    request.user_id,
                    session_id,
                    request.expected_version,
                ),
            )

            if result.rowcount != 1:
                raise StateError(
                    "version_conflict",
                    "条件版本冲突，更新已回滚。",
                )

            return {
                "changed": True,
                "task": self.task_data(
                    self._task(conn, request.user_id, session_id)
                ),
            }

    def start_run(self, session_id, request: StartRun, limits=None,
                  execution_mode="m1_simulation"):
        if execution_mode not in ("m1_simulation", "m2_live", "m3_offline", "m3_live", "m4_offline", "m4_llm_check", "m4_live", "m5_live", "m6_live"):
            raise StateError("invalid_execution_mode", "不支持此运行方式。", 422)
        request = StartRun.model_validate(request)
        limits = BudgetLimits.model_validate(limits or BudgetLimits())

        rid, timestamp = uuid4().hex, now()

        with self.write() as conn:
            task = self._task(conn, request.user_id, session_id)
            self.check_version(task, request.expected_version)

            if task["active_run_id"] is not None:
                raise StateError(
                    "run_already_active",
                    "已有运行，请读取并继续该运行。",
                )

            conn.execute(
                "INSERT INTO run_metrics VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    rid, session_id, request.expected_version, execution_mode, "running",
                    limits.model_dump_json(),
                    BudgetCounters().model_dump_json(),
                    None,
                    timestamp,
                    timestamp,
                ),
            )

            conn.execute(
                """
                UPDATE task_sessions
                SET phase='running', active_run_id=?, updated_at=?
                WHERE session_id=?
                """,
                (rid, timestamp, session_id),
            )

            return self.run_data(
                self._run(conn, request.user_id, rid)
            )

    def get_run(self, user_id, run_id):
        with self.connection() as conn:
            return self.run_data(
                self._run(conn, user_id, run_id)
            )

    def assert_run_current(self, user_id, run_id):
        # M4 接收工具结果和生成最终结果前，要再次检查版本。
        with self.connection() as conn:
            run = self._run(conn, user_id, run_id)
            task = self._task(conn, user_id, run["session_id"])

            self.check_current(task, run)

            return self.run_data(run)

    def reserve(self, user_id, run_id, reservation: Reservation):
        reservation = Reservation.model_validate(reservation)
        blocked = None

        with self.write() as conn:
            run = self._run(conn, user_id, run_id)
            task = self._task(conn, user_id, run["session_id"])
            self.check_current(task, run)

            limits = BudgetLimits.model_validate_json(
                run["limits_json"]
            )
            counters = BudgetCounters.model_validate_json(
                run["counters_json"]
            )

            proposed = counters.model_dump()
            resource, count = reservation.resource, reservation.count

            if resource == "search":
                # 新一轮搜索同时占用一轮搜索和一次地图请求许可。
                proposed["search_rounds"] += 1
                proposed["map_http"] += 1
                proposed["candidates_in_round"] = 0

            elif resource == "map_http":
                proposed["map_http"] += 1

            elif resource == "llm":
                proposed["llm_calls"] += 1

            else:
                if counters.search_rounds == 0:
                    raise StateError(
                        "search_not_started",
                        "先开启搜索轮次，再登记新增候选。",
                    )

                proposed["candidates_in_round"] += count
                proposed["candidates_total"] += count

            checks = (
                ("search_rounds", limits.max_search_rounds),
                ("map_http", limits.max_map_http),
                ("llm_calls", limits.max_llm_calls),
                (
                    "candidates_in_round",
                    limits.max_candidates_per_round,
                ),
            )

            exceeded = next(
                (
                    key
                    for key, limit in checks
                    if proposed[key] > limit
                ),
                None,
            )

            if exceeded:
                # 单轮候选满额只拒绝加候选。
                # 全局预算不足则停止本次运行。
                if exceeded != "candidates_in_round":
                    timestamp = now()

                    conn.execute(
                        """
                        UPDATE run_metrics
                        SET status='stopped',
                            stop_reason=?,
                            updated_at=?
                        WHERE run_id=?
                        """,
                        (
                            "budget_exhausted:" + exceeded,
                            timestamp,
                            run_id,
                        ),
                    )

                    conn.execute(
                        """
                        UPDATE task_sessions
                        SET phase='stopped',
                            active_run_id=NULL,
                            updated_at=?
                        WHERE session_id=?
                        """,
                        (timestamp, run["session_id"]),
                    )

                blocked = StateError(
                    "budget_exhausted",
                    "预算不足，本次动作未获许可。",
                    resource=exceeded,
                )

            else:
                validated = BudgetCounters.model_validate(proposed)

                conn.execute(
                    """
                    UPDATE run_metrics
                    SET counters_json=?, updated_at=?
                    WHERE run_id=?
                    """,
                    (
                        validated.model_dump_json(),
                        now(),
                        run_id,
                    ),
                )

                granted = self.run_data(
                    self._run(conn, user_id, run_id)
                )

        # 先提交停止状态，再抛出错误，避免停止状态被回滚。
        if blocked is not None:
            raise blocked

        return {
            "granted": True,
            "run": granted,
        }

    def finish_run(self, user_id, run_id, reason="manual_stop"):
        allowed = {
            "manual_stop", "demo_finished", "tools_checked",
            "needs_clarification", "no_place", "no_candidates",
            "no_route", "data_missing", "api_error", "feasible_target_reached",
            "budget_exhausted:map_http", "budget_exhausted:search_rounds",
            "agent_finished", "no_legal_actions", "model_output_invalid", "llm_error", "budget_exhausted:llm_calls",
        }
        if reason not in allowed:
            raise StateError("invalid_finish_reason", "不接受此结束原因。", 422)
        status = "stopped" if reason.startswith("budget_exhausted:") else "finished"
        with self.write() as conn:
            run = self._run(conn, user_id, run_id)
            task = self._task(conn, user_id, run["session_id"])
            self.check_current(task, run)
            timestamp = now()
            conn.execute("""UPDATE run_metrics SET status=?, stop_reason=?,
                updated_at=? WHERE run_id=?""", (status, reason, timestamp, run_id))
            conn.execute("""UPDATE task_sessions SET phase='stopped', active_run_id=NULL,
                updated_at=? WHERE session_id=?""", (timestamp, run["session_id"]))
            return self.run_data(self._run(conn, user_id, run_id))

    def inspect(self):
        with self.connection() as conn:
            return {
                "schema_version": conn.execute(
                    "PRAGMA user_version"
                ).fetchone()[0],
                "tables": [
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type='table'
                        ORDER BY name
                        """
                    )
                ],
                "task_count": conn.execute(
                    "SELECT COUNT(*) FROM task_sessions"
                ).fetchone()[0],
                "run_count": conn.execute(
                    "SELECT COUNT(*) FROM run_metrics"
                ).fetchone()[0],
            }