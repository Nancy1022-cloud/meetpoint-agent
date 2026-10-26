import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.config import Settings
from app.errors import M0Error

CHINA_TZ = timezone(timedelta(hours=8))


def check_location(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"-?\d{1,3}(?:\.\d{1,6})?,-?\d{1,2}(?:\.\d{1,6})?", value):
        raise M0Error("bad_location", "坐标应为经度,纬度，小数不超过6位；请复制查询返回的 location。")
    longitude, latitude = map(float, value.split(","))
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise M0Error("bad_location", "坐标超出合法经纬度范围。")
    return value


def nonnegative_int(value):
    # 缺失、空字符串、空数组和不合法数字都保持未知，不能用0代替。
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return int(text) if text.isdigit() else None


class AmapClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.Client(timeout=settings.amap_timeout_seconds)
        self.http_requests = 0
        self.last_started = None

    def close(self):
        self.http.close()

    def _get(self, path: str, params: dict) -> dict:
        key = self.settings.amap_web_key.get_secret_value().strip()
        if not key:
            raise M0Error("missing_amap_key", "请在 .env 中填写 AMAP_WEB_KEY。")
        if self.http_requests >= 4:
            raise M0Error("m0_http_limit", "本次 M0 命令达到4次地图 HTTP 请求上限。")
        if self.last_started is not None:
            delay = self.settings.amap_min_interval_seconds - (time.monotonic() - self.last_started)
            if delay > 0:
                time.sleep(delay)
        self.last_started = time.monotonic()
        self.http_requests += 1
        try:
            response = self.http.get(
                "https://restapi.amap.com" + path, params={**params, "key": key}
            )
        except httpx.TimeoutException:
            raise M0Error("amap_timeout", "高德请求超时；未自动重试，请检查网络后单独重测。") from None
        except httpx.RequestError:
            raise M0Error("amap_network", "无法连接高德；请检查本机或 Docker 网络。") from None
        if response.status_code != 200:
            raise M0Error("amap_http_error", f"高德返回 HTTP {response.status_code}，请检查权限或服务状态。")
        try:
            data = response.json()
        except ValueError:
            raise M0Error("amap_bad_json", "高德响应不是合法 JSON。") from None
        if not isinstance(data, dict):
            raise M0Error("amap_bad_shape", "高德响应结构与文档不符。")
        if str(data.get("status")) != "1" or str(data.get("infocode")) != "10000":
            code = str(data.get("infocode", "unknown"))
            # 不回显请求 URL、Key、原始错误体或可能含 Key 的异常文本。
            code = code if re.fullmatch(r"\d{5}", code) else "unknown"
            info = data.get("info")
            info = info if isinstance(info, str) and re.fullmatch(r"[A-Z_]{1,80}", info) else "API_ERROR"
            raise M0Error("amap_api_error", f"高德业务错误：infocode={code}，info={info}。核对 Key、接口权限和配额。")
        return data

    def search(self, query: str, city: str) -> dict:
        if not (1 <= len(query.strip()) <= 80) or city != self.settings.m0_city:
            raise M0Error("invalid_place_args", "地点关键字应为1至80字，城市必须是本次配置的测试城市。")
        data = self._get("/v5/place/text", {
            "keywords": query.strip(), "region": city, "city_limit": "true",
            "page_size": 3, "page_num": 1, "output": "json",
        })
        pois = data.get("pois")
        if not isinstance(pois, list):
            raise M0Error("place_data_missing", "地点接口缺少 pois 数组。")
        if not pois:
            raise M0Error("no_place", "接口正常，但没有搜索到地点；请换成明确的地铁站名称。")
        candidates = []
        for poi in pois:
            if not isinstance(poi, dict):
                continue
            if not all(isinstance(poi.get(k), str) and poi[k].strip() for k in ("id", "name", "location")):
                continue
            try:
                location = check_location(poi["location"])
            except M0Error:
                continue
            candidates.append({
                "poi_id": poi["id"], "name": poi["name"], "location": location,
                "address": poi.get("address"), "cityname": poi.get("cityname"),
                "typecode": poi.get("typecode"),
            })
        if not candidates:
            raise M0Error("place_data_missing", "找到地点，但缺少可用的 id、name 或 location。")
        return {
            "query": query.strip(), "city": city, "candidates": candidates,
            "skipped_incomplete_count": len(pois) - len(candidates),
            "needs_user_confirmation": True,
        }

    def transit(self, origin: str, destination: str) -> dict:
        origin, destination = check_location(origin), check_location(destination)
        # M0 用北京时间当天10:00探测，避免深夜测试造成停运干扰；非实时出发承诺。
        departure_date = datetime.now(CHINA_TZ).date().isoformat()
        data = self._get("/v5/direction/transit/integrated", {
            "origin": origin, "destination": destination,
            "city1": self.settings.m0_citycode, "city2": self.settings.m0_citycode,
            "strategy": 0, "AlternativeRoute": 3, "show_fields": "cost",
            "date": departure_date, "time": "10-00", "output": "json",
        })
        route = data.get("route")
        if not isinstance(route, dict) or not isinstance(route.get("transits"), list):
            raise M0Error("route_data_missing", "缺少 route.transits 数组，不能当作没有路线。")
        transits = route["transits"]
        if not transits:
            raise M0Error("no_route", "接口正常，但当前参数没有返回公交方案；请先核对两端地点和城市编码。")
        routes = []
        for index, transit in enumerate(transits, start=1):
            if not isinstance(transit, dict):
                continue
            cost = transit.get("cost")
            cost = cost if isinstance(cost, dict) else {}
            duration = nonnegative_int(cost.get("duration"))
            routes.append({
                "index": index, "duration_seconds": duration,
                "duration_minutes": round(duration / 60, 1) if duration is not None else None,
                "distance_meters": nonnegative_int(transit.get("distance")),
                "duration_field": "route.transits[].cost.duration",
                "duration_available": duration is not None and duration > 0,
            })
        if not any(item["duration_available"] for item in routes):
            raise M0Error("duration_missing", "有公交方案，但未取得合法的 cost.duration；不能按0分钟通过。请核对 show_fields=cost 及接口版本。")
        return {
            "api_version": "v5", "departure_date": departure_date, "departure_time": "10-00",
            "route_count_received": len(transits), "routes": routes,
            "note": "M0仅验收路线和耗时；步行及换乘口径在M2实现。",
        }
