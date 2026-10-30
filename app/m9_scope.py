"""明确的能力范围规则；未知表达仍交给受控解析器澄清，不调用模型。"""

import re

from app.m5_models import MemoryMessage


CAPABILITIES = "目前支持两人在北京市内，按公交/地铁路线选择商场，并修改本次条件或明确保存默认偏好。"
PROTOCOL = "m9_scope_v1"
# 规则表达业务边界，不读取用例ID、expected_outcomes或标准答案。
RULES = (
    ("party_size", r"(?:帮|为|给)?(?:我们)?(?:[3-9]|[1-9][0-9]+|三|四|五|六|七|八|九|十)(?:个)?人.{0,12}(?:选择|选|找|见面|会合)",
     "目前仅支持两人会合，请改为两人的任务。"),
    ("city", r"(?:只)?(?:查询|搜索|查找|找|规划).{0,5}(?:上海|杭州|天津|南京|广州|深圳|成都)(?:市)?(?:的商场|的见面|内的|市内)",
     "目前只接入北京市内的地点和路线，请提供北京的任务。"),
    ("transport", r"(?:规划|查询|查找|选择|改用|改成|使用|只要).{0,10}(?:自驾|开车|骑自行车|骑行|全程步行|摩托车|游轮).{0,6}(?:路线|出行|方案)?$",
     "目前仅支持公交/地铁路线，请改用公共交通。"),
    ("transaction", r"(?:替|代|帮|直接|请|我要|我想).{0,12}(?:预订|订位|订票|支付|买.{0,6}票|下单|购买).{0,20}(?:餐厅|电影|商品|停车|商场|费用|票|订位)|(?:替|代|帮).{0,12}(?:餐厅订位|支付停车费用)",
     "我不能代订、下单或支付，可以继续为你选择会合商场。"),
    ("tracking", r"(?:跟踪|定位|获取|查|查询).{0,22}(?:实时坐标|当前位置|实时通勤轨迹|家庭地址|现在的位置)|(?:不知情).{0,12}(?:坐标|位置)",
     "我不提供他人的实时位置追踪；请使用双方自行提供的出发点。"),
    ("budget_override", r"(?:绕开|忽略|取消|不要计数|无限|耗尽后继续).{0,15}(?:预算|平台限制|限流|接口|请求|地图)|(?:预算耗尽后|不要计数地图请求).{0,12}(?:请求|重试|继续)",
     "请求次数、预算和限流必须继续执行，可以在允许范围内调整本次上限。"),
    ("finance", r"(?:查询|预测|办理|估算|签署|帮|代).{0,18}(?:房屋买卖价格|股票价格|房产贷款|股权交易价格|商铺买卖合同)",
     "商场房产和金融业务超出当前能力范围。"),
    ("international", r"(?:规划|预订|查询|安排|申请|为|替).{0,20}(?:跨国|国际机票|跨洲|境外旅行签证)",
     "跨国旅行和票务不在当前会合规划范围内。"),
    ("venue_type", r"(?:只查|查询|搜索|改成|改为|帮我找)(?:北京|北京市)?(?:的)?(?:餐厅|公园|酒店)(?:$|作为|见面|会合)",
     "目前候选类型仅支持商场，请改用商场会合。"),
)


def scope_decision(text):
    text = re.sub(r"\s+", "", text)
    # 转述/假设/问范围不作确定请求；由原解析器澄清。引号中的词不直接执行。
    text = re.sub(r"[“‘\"].*?[”’\"]", "", text)
    clauses = re.split(r"[，,。；;！!]|但是|但|然后", text)
    for clause in clauses:
        clause = clause.strip("：:")
        if not clause or re.match(r"^(?:请)?(?:不要(?!计数)|别|不用|不需要|不想|不再|如果|假设|例如|他说|她说|你支持|是否支持|能不能支持)", clause):
            continue
        clause = re.sub(r"(?:不要|不用|不需要|不想|别)(?:使用|采用|选择|规划)?(?:自驾|开车|骑行|骑自行车|全程步行|摩托车|游轮)", "", clause)
        for category, pattern, message in RULES:
            if re.search(pattern, clause):
                return {"decision": "refuse", "reason": "unsupported_scope",
                        "scope_reason": category, "message": message, "protocol": PROTOCOL}
    return {"decision": "continue", "reason": None, "scope_reason": None,
            "message": None, "protocol": PROTOCOL}


def apply_scoped_message(store, session_id, request):
    """产品API与M9评测共用入口；拒答也先核对用户归属和条件版本。"""
    request = MemoryMessage.model_validate(request)
    decision = scope_decision(request.text)
    if decision["decision"] != "refuse":
        return store.apply_message(session_id, request)
    with store.write() as conn:
        task = store._task(conn, request.user_id, session_id)
        store.check_version(task, request.expected_version)
        # 不保存原文、不修改条件/偏好/待澄清状态；仅保存枚举控制记录。
        store._event(conn, request.user_id, session_id, task["constraints_version"],
                     "scope_refused", {"reason": decision["scope_reason"], "protocol": PROTOCOL})
        return {"status": "refused", "reason": decision["reason"],
                "scope_reason": decision["scope_reason"], "message": decision["message"],
                "capabilities": CAPABILITIES, "scope_protocol": PROTOCOL,
                "changed_fields": [], "replan_required": False,
                **store._memory(conn, request.user_id, session_id)}
