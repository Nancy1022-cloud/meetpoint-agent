from pydantic import ValidationError

from app.m2_models import (
    Place, PlaceResult, TransitResult, TransitRoute, nonnegative_int,
)

# 购物中心、普通商场。排除购物服务大类、免税店及无法确认的泛类。
MALL_TYPES = frozenset({"060101", "060102"})


def parse_places(data, mall_only=False):
    pois = data.get("pois")
    if not isinstance(pois, list):
        return PlaceResult(status="data_missing", rejected_incomplete=1)
    if not pois:
        return PlaceResult(status="no_candidates" if mall_only else "no_place")
    accepted, incomplete, category, city = [], 0, 0, 0
    seen = set()
    for raw in pois:
        if not isinstance(raw, dict):
            incomplete += 1
            continue
        code = raw.get("citycode")
        if not isinstance(code, str) or not code.strip():
            incomplete += 1
            continue
        if code.strip() != "010":
            city += 1
            continue
        typecode = raw.get("typecode")
        if mall_only:
            if not isinstance(typecode, str) or not typecode.strip():
                incomplete += 1
                continue
            if not (set(typecode.split("|")) & MALL_TYPES):
                category += 1
                continue
        try:
            place = Place(
                poi_id=raw.get("id"), name=raw.get("name"),
                location=raw.get("location"), citycode=code.strip(),
                typecode=typecode if isinstance(typecode, str) else None,
                address=raw.get("address") if isinstance(raw.get("address"), str) else None,
            )
        except (ValidationError, ValueError):
            incomplete += 1
            continue
        if place.poi_id not in seen:
            accepted.append(place)
            seen.add(place.poi_id)
    status = "ok" if accepted else (
        "data_missing" if incomplete else "no_candidates" if mall_only else "no_place"
    )
    return PlaceResult(
        status=status, places=accepted, rejected_incomplete=incomplete,
        rejected_category=category, rejected_city=city,
    )


def parse_transits(data):
    root = data.get("route")
    if not isinstance(root, dict) or not isinstance(root.get("transits"), list):
        return TransitResult(status="data_missing", issues=["route.transits"])
    transits = root["transits"]
    if not transits:
        # 显式空列表才是无路线；字段缺失不走这里。
        return TransitResult(status="no_route")
    routes = []
    for index, raw in enumerate(transits, 1):
        if not isinstance(raw, dict):
            routes.append(TransitRoute(route_id=f"route_{index}",
                missing_fields=["duration_seconds", "walk_meters", "transfers"]))
            continue
        cost = raw.get("cost")
        duration = nonnegative_int(cost.get("duration")) if isinstance(cost, dict) else None
        if duration == 0:
            duration = None
        walk = nonnegative_int(raw.get("walking_distance"))
        walking_source = "transit.walking_distance" if walk is not None else None
        segments = raw.get("segments")
        segment_walk, walk_known = 0, True
        boardings, modes_known, options, issues = 0, True, [], []
        if not isinstance(segments, list) or not segments:
            walk_known, modes_known = False, False
        else:
            for segment in segments:
                if not isinstance(segment, dict):
                    walk_known, modes_known = False, False
                    continue
                walking = segment.get("walking")
                distance = (nonnegative_int(walking.get("distance"))
                            if isinstance(walking, dict) else None)
                if distance is None:
                    walk_known = False
                else:
                    segment_walk += distance
                # 本版仅接收公交/地铁和步行；火车或打车段不能被忽略后算作0换乘。
                if segment.get("railway") or segment.get("taxi"):
                    modes_known = False
                    issues.append("unsupported_mode:railway_or_taxi")
                bus = segment.get("bus")
                buslines = bus.get("buslines") if isinstance(bus, dict) else None
                has_bus = isinstance(buslines, list) and bool(buslines)
                if has_bus:
                    # 同一段可有多条可选线路，但只需乘坐其中一条，故上车数只加1。
                    boardings += 1
                    names = [line["name"] for line in buslines
                             if isinstance(line, dict) and isinstance(line.get("name"), str)
                             and line["name"].strip()]
                    options.append(names)
                    if len(names) != len(buslines):
                        issues.append("busline_name_missing")
                elif bus and not (isinstance(buslines, list) and not buslines):
                    # 非空的 bus 却没有可识别的 buslines，不能猜测乘车次数。
                    modes_known = False
                elif distance is None:
                    modes_known = False
        if walk is None and walk_known:
            walk, walking_source = segment_walk, "sum(segments[].walking.distance)"
        elif walk is not None and walk_known and walk != segment_walk:
            issues.append("walking_total_mismatch")
        transfers = max(boardings - 1, 0) if modes_known else None
        missing = [name for name, value in (
            ("duration_seconds", duration), ("walk_meters", walk), ("transfers", transfers),
        ) if value is None]
        routes.append(TransitRoute(
            route_id=f"route_{index}", duration_seconds=duration, walk_meters=walk,
            boardings=boardings if modes_known else None, transfers=transfers,
            line_options=options, walking_source=walking_source,
            missing_fields=missing, issues=sorted(set(issues)),
        ))
    return TransitResult(status="ok" if any(r.complete for r in routes) else "data_missing",
                         routes=routes)