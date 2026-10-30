import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from app.m1_models import Reservation
from app.m2_models import MapToolError


class RateGate:
    """同一项目、同一 gate 文件内跨进程串行；文件只保存上次请求时间。"""
    def __init__(self, path, interval, clock=time.time, sleeper=time.sleep):
        self.path, self.interval = Path(path), interval
        self.clock, self.sleeper = clock, sleeper
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=120) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS request_gate ("
                         "id INTEGER PRIMARY KEY CHECK(id=1), last_start REAL NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO request_gate VALUES (1, 0)")

    @contextmanager
    def slot(self):
        conn = sqlite3.connect(self.path, timeout=120, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            last = conn.execute("SELECT last_start FROM request_gate WHERE id=1").fetchone()[0]
            # 系统时钟回拨时最多等待一个间隔，避免产生异常长等待。
            delay = min(self.interval, max(0, last + self.interval - self.clock()))
            if delay:
                self.sleeper(delay)
            yield conn
            conn.commit()
        except BaseException:
            # 即使本次请求被中断，也保留已经登记的开始时间。
            conn.commit()
            raise
        finally:
            conn.close()

    def mark_started(self, conn):
        conn.execute("UPDATE request_gate SET last_start=? WHERE id=1", (self.clock(),))


class MapClient:
    PATHS = frozenset({"/v5/place/text", "/v5/place/around",
                       "/v5/direction/transit/integrated"})
    RATE_CODES = frozenset({"10004", "10014", "10015", "10019", "10020", "10021", "10029"})
    AUTH_CODES = frozenset({"10001", "10002", "10005", "10006", "10007", "10008",
                           "10009", "10011", "10012", "10013", "10026", "10041", "40002"})
    QUOTA_CODES = frozenset({"10003", "10010", "10044", "10045", "40000", "40003"})

    def __init__(self, settings, store, user_id, run_id, *, transport=None, gate=None):
        self.key = settings.amap_web_key.get_secret_value().strip()
        if not self.key:
            raise MapToolError("missing_amap_key", "请在 .env 中填写高德 Web 服务 Key。")
        self.store, self.user_id, self.run_id = store, user_id, run_id
        self.http = httpx.Client(timeout=settings.amap_timeout_seconds,
                                 transport=transport, follow_redirects=False)
        self.gate = gate or RateGate(store.path.parent / "amap_rate_gate.sqlite3",
                                    settings.amap_min_interval_seconds)
        self.attempts_started, self.retries_started = 0, 0
        # httpx 的 INFO 日志会含请求URL；本项目只输出安全错误码与计数。
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    def close(self):
        self.http.close()

    def _decode(self, response):
        status = response.status_code
        if status != 200:
            retry = status == 429 or 500 <= status <= 599
            code = "http_rate_limited" if status == 429 else (
                "http_server_error" if 500 <= status <= 599 else "http_error"
            )
            raise MapToolError(code, "高德 HTTP 请求失败。", retryable=retry, http_status=status)
        try:
            data = response.json()
        except ValueError:
            raise MapToolError("data_missing", "响应不是合法 JSON。") from None
        if not isinstance(data, dict):
            raise MapToolError("data_missing", "响应缺少对象结构。")
        if str(data.get("status")) == "1" and str(data.get("infocode")) == "10000":
            return data
        raw = str(data.get("infocode", ""))
        code = raw if len(raw) == 5 and raw.isascii() and raw.isdigit() else None
        if code in self.AUTH_CODES:
            error = MapToolError("amap_auth_error", "高德 Key、平台或权限配置有误。", infocode=code)
        elif code in self.QUOTA_CODES:
            error = MapToolError("amap_quota_exhausted", "高德调用额度、余额或 IP 限额不足。", infocode=code)
        elif code in self.RATE_CODES:
            error = MapToolError("amap_rate_limited", "高德频率限制。", True, code)
        elif code == "10016":
            error = MapToolError("amap_service_error", "高德服务繁忙。", True, code)
        elif code and code.startswith("200"):
            error = MapToolError("amap_parameter_error", "高德拒绝请求参数。", infocode=code)
        elif code and code.startswith("208"):
            error = MapToolError("amap_route_error", "高德算路失败；不能当作成功的空路线。", infocode=code)
        else:
            error = MapToolError("amap_api_error", "高德业务请求失败。", infocode=code)
        raise error

    def get(self, path, params):
        if path not in self.PATHS:
            raise MapToolError("invalid_tool_path", "执行器拒绝未注册接口。")
        # 周边搜索的第一发同时扣轮数+HTTP；其重试只扣HTTP。
        for attempt in range(2):
            resource = "search" if path == "/v5/place/around" and attempt == 0 else "map_http"
            error = None
            try:
                with self.gate.slot() as gate_conn:
                    self.store.reserve(self.user_id, self.run_id, Reservation(resource=resource))
                    self.gate.mark_started(gate_conn)
                    self.attempts_started += 1
                    self.retries_started += int(attempt > 0)
                    try:
                        response = self.http.get("https://restapi.amap.com" + path,
                                                 params={**params, "key": self.key})
                    except httpx.TimeoutException:
                        error = MapToolError("timeout", "高德请求超时。", True)
                    except httpx.RequestError:
                        error = MapToolError("network_error", "高德连接失败，请检查 Docker 网络。")
            except sqlite3.OperationalError:
                raise MapToolError("local_state_busy", "本机限流或状态数据库繁忙，请停止并发操作后重试。") from None
            self.store.assert_run_current(self.user_id, self.run_id)
            if error is None:
                try:
                    return self._decode(response)
                except MapToolError as exc:
                    error = exc
            if error.retryable and attempt == 0:
                # 退避后第二发仍须通过共享限流和预算检查；不自动尝试第三发。
                time.sleep(1.1)
                continue
            raise error