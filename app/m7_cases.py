"""M7 预先登记的自建数据。作者可见，不称独立盲测；完全不含地图响应。"""

from copy import deepcopy


def route(seconds=1800, walk=500, transfers=1):
    return {"seconds": seconds, "walk": walk, "transfers": transfers}


def candidate(cid, x, a=None, b=None):
    # x 是自建平面中的米；只是方便测试搜索半径，不模拟真实路网。
    return {"id": cid, "x": x, "a": [route()] if a is None else a,
            "b": [route()] if b is None else b}


def cluster(prefix, x, seconds=1800):
    return [candidate(f"{prefix}{i}", x + 100*i,
                      [route(seconds + 60*i)], [route(seconds - 60*i)]) for i in range(3)]


def requirements(**extra):
    return {"person_a": {"origin_text": "合成起点A"},
            "person_b": {"origin_text": "合成起点B"}, **extra}


def planning_cases():
    bad_a = lambda cid="bad", x=0: candidate(cid, x, [route(3300)])
    bad_b = lambda cid="bad", x=0: candidate(cid, x, b=[route(3300)])
    definitions = [
        ("S01", "首轮足够，双方不需要决策", True, cluster("near", -100)),
        ("S02", "A侧负担，A侧区域有解", True, [bad_a()] + cluster("west", -6000)),
        ("S03", "B侧负担，B侧区域有解", True, [bad_b()] + cluster("east", 5800)),
        ("S04", "首轮无路线，扩大半径有解", True,
         [candidate("noroute", 0, a=[])] + cluster("outer", 2300)),
        ("S05", "方向提示有误，B1多查一轮", True,
         [bad_a()] + cluster("outer", 2300)),
        ("S06", "误导性反馈使B1漏掉半径5km的解", True,
         [bad_a(), bad_a("decoy", -5000)] + cluster("outer", 4200)),
        ("S07", "双方都交付，方案质量不同", True,
         [candidate("ordinary", 0, [route(2400)], [route(2400)]), bad_a("bad", 200)]
         + cluster("better", -6000, 1800)),
        ("S08", "自建全集中无满足时间约束的解", False,
         [bad_a("bad1", 0), bad_a("bad2", -5000), bad_b("bad3", 5000)]),
        ("S09", "必需耗时未知，不能当0", False,
         [candidate("unknown", 0, [route(None)])]),
        ("S10", "双方路线至少一侧不存在", False,
         [candidate("no_route", 0, a=[])]),
        ("S11", "临界上限及多条备选路线", True,
         [candidate("boundary", 0, [route(1200, 2000), route(2700, 1500)],
                    [route(2700, 1500)]),
          candidate("unknown", 200, [route(None)]),
          candidate("transfers", 400, [route(1200, 200, 2)])]),
        ("S12", "HTTP预算中断第二个候选，只交付完整项", True,
         [candidate("complete", 0), candidate("partial", 100)]),
    ]
    return [{"id": cid, "title": title, "expected_has_feasible": feasible,
             "requirements": requirements(), "candidates": values,
             "limits": {"max_map_http": 6 if cid == "S12" else 40,
                        "max_llm_calls": 12, "max_search_rounds": 3,
                        "max_candidates_per_round": 5}}
            for cid, title, feasible, values in definitions]


def memory_cases():
    new = {"op": "new", "a": {"origin_text": "合成起点A"},
           "b": {"origin_text": "合成起点B"}}
    say = lambda text: {"op": "say", "text": text}
    filler = [say("朋友最多步行1000米"), say("均衡优先"), say("朋友最多换乘1次")]
    definitions = [
        ("R01", "保留窗口外A时间", [new, say("我最多60分钟")] + filler,
         {"person_a.max_duration_seconds": 3600, "person_b.max_walk_meters": 1000}, {}, False),
        ("R02", "只改A仍保留B的旧上限", [new, say("朋友最多40分钟")] + filler
         + [say("我这次可以接受60分钟")],
         {"person_a.max_duration_seconds": 3600, "person_b.max_duration_seconds": 2400,
          "person_b.max_walk_meters": 1000}, {}, False),
        ("R03", "本次覆盖不污染长期偏好",
         [{"op": "save", "values": {"max_transfers": 0}}, new, say("我这次最多换乘2次")],
         {"person_a.max_transfers": 2}, {"max_transfers": 0}, False),
        ("R04", "新会话继承A偏好，B不继承",
         [new, say("以后默认最多换乘0次"), new],
         {"person_a.max_transfers": 0}, {"max_transfers": 0}, False),
        ("R05", "新会话显式条件优先于长期偏好",
         [{"op": "save", "values": {"max_duration_seconds": 3600}},
          {**new, "a": {"origin_text": "合成起点A", "max_duration_seconds": 2700}}],
         {}, {"max_duration_seconds": 3600}, False),
        ("R06", "否定句不触发部分写入", [new, say("不要把我改成60分钟")], {}, {}, True),
        ("R07", "删除偏好后新会话使用默认",
         [{"op": "save", "values": {"max_transfers": 0}}, new,
          say("删除我的全部长期偏好"), new], {}, {}, False),
        ("R08", "起点更新不能随窗口丢失", [new, say("我从合成起点C出发")] + filler,
         {"person_a.origin_text": "合成起点C", "person_b.max_walk_meters": 1000}, {}, False),
    ]
    return [{"id": cid, "title": title, "events": deepcopy(events),
             "expected_changes_from_defaults": expected, "expected_preferences": prefs,
             "expected_pending": pending} for cid, title, events, expected, prefs, pending in definitions]


def suite():
    return {"schema_version": 1, "data_source": "self_authored_synthetic",
            "independent_blind_holdout": False, "planning": planning_cases(),
            "memory": memory_cases()}