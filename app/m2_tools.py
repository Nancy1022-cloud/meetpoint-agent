from datetime import date, time

from app.m1_models import Reservation
from app.m2_models import MapToolError, location_text
from app.m2_parser import MALL_TYPES, parse_places, parse_transits


class MapTools:
    def __init__(self, client, departure_date, departure_time):
        # 参数先校验，避免无效参数消耗地图配额。
        parsed_date = date.fromisoformat(departure_date)
        parsed_time = time.fromisoformat(departure_time)
        self.departure_date = parsed_date.isoformat()
        self.departure_time = parsed_time.strftime("%H-%M")
        self.client = client
        # 仅本次进程的临时上下文，不放SQLite或未来的持久图状态。
        self.resolutions, self.origins, self.candidates = {}, {}, {}

    def resolve_place(self, role, query):
        if role not in ("a", "b") or not isinstance(query, str) or not 1 <= len(query.strip()) <= 80:
            raise MapToolError("invalid_place_args", "起点角色或文字不合法。")
        data = self.client.get("/v5/place/text", {
            "keywords": query.strip(), "region": "北京市", "city_limit": "true",
            "page_size": 5, "page_num": 1, "output": "json",
        })
        result = parse_places(data)
        self.resolutions[role] = {place.poi_id: place for place in result.places}
        return result

    def confirm_origin(self, role, poi_id):
        place = self.resolutions.get(role, {}).get(poi_id)
        if place is None:
            raise MapToolError("unverified_origin", "起点必须来自本次地点解析结果。")
        self.origins[role] = place
        return place

    def midpoint(self):
        if set(self.origins) != {"a", "b"}:
            raise MapToolError("needs_clarification", "请先确认双方起点。")
        a = list(map(float, self.origins["a"].location.split(",")))
        b = list(map(float, self.origins["b"].location.split(",")))
        return f"{(a[0] + b[0]) / 2:.6f},{(a[1] + b[1]) / 2:.6f}"

    def search_meeting_places(self, center, radius=1500):
        # M2 联调只用中间区域。后续M4执行器负责计算其它合法搜索动作的中心。
        location_text(center)
        if set(self.origins) != {"a", "b"}:
            raise MapToolError("needs_clarification", "请先确认双方起点。")
        if type(radius) is not int or not 1 <= radius <= 50000:
            raise MapToolError("invalid_radius", "搜索半径应为1至50000米的整数。")
        data = self.client.get("/v5/place/around", {
            "location": center, "radius": radius, "types": "|".join(sorted(MALL_TYPES)),
            "region": "北京市", "city_limit": "true", "sortrule": "distance",
            "page_size": 25, "page_num": 1, "output": "json",
        })
        result = parse_places(data, mall_only=True)
        run = self.client.store.assert_run_current(self.client.user_id, self.client.run_id)
        allowance = run["remaining"]["candidates_in_round"]
        # 保留官方距离排序；同样输入按相同顺序截取。跨轮只接纳新POI。
        fresh = [p for p in result.places if p.poi_id not in self.candidates][:allowance]
        if fresh:
            self.client.store.reserve(self.client.user_id, self.client.run_id,
                                      Reservation(resource="candidates", count=len(fresh)))
            self.candidates.update({place.poi_id: place for place in fresh})
        result = result.model_copy(update={"places": fresh})
        if result.status == "ok" and not fresh:
            result = result.model_copy(update={"status": "no_candidates"})
        return result

    def get_transit_routes(self, role, candidate_id):
        origin = self.origins.get(role)
        destination = self.candidates.get(candidate_id)
        if origin is None or destination is None:
            raise MapToolError("unverified_route_endpoint", "路线端点必须是已确认起点和本次搜索到的候选。")
        data = self.client.get("/v5/direction/transit/integrated", {
            "origin": origin.location, "destination": destination.location,
            "originpoi": origin.poi_id, "destinationpoi": destination.poi_id,
            "city1": "010", "city2": "010", "strategy": 0,
            "AlternativeRoute": 3, "show_fields": "cost",
            "date": self.departure_date, "time": self.departure_time, "output": "json",
        })
        return parse_transits(data)