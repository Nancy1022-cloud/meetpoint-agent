"""受控句式解析器：完整匹配才修改，含糊/否定/混合作用域一律澄清。

这不是通用自然语言理解；M5验证Memory行为，不额外请求模型。
原始消息只在本次函数调用内使用，持久化仅保存结构化变更。
"""

import re

from pydantic import ValidationError

from app.m1_models import RequirementsPatch
from app.m5_models import PreferenceValues

QUESTION = "请明确修改谁、哪个条件和新值；本次条件与以后默认值请分开说。"
NUMBERS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
ROLE = r"(我|朋友|双方|两人|[ABab])"
LEAD = r"(?:这次|本次)?(?:仍然|仍|也)?(?:可以接受|能接受|接受|最多|改为|改成|设为)?"


def number(text):
    return int(text) if text.isascii() and text.isdigit() else NUMBERS[text]


def clarify():
    return {"intent": "clarify", "question_code": "explicit_change", "question": QUESTION}


def parse_message(text):
    text = re.sub(r"\s+", "", text).strip("，,。；;")
    if text in ("删除我的全部长期偏好", "忘记我的全部长期偏好", "清空我的长期偏好"):
        return {"intent": "delete_preferences", "values": {}}
    # 仅识别确定声明；不从否定、假设、转述中截取数字。
    if any(word in text for word in ("不要", "不用", "不是", "别", "如果", "要不", "？", "?", "记得吗")):
        return clarify()
    scope = "current"
    prefix = re.match(r"^(以后默认|以后请记住|以后记住|请记住|记住我以后默认|今后默认)", text)
    if prefix:
        scope, text = "preferences", text[prefix.end():]
    if not text or any(word in text for word in ("以后", "默认", "记住", "下次", "今后")):
        return clarify()
    if scope == "preferences" and any(word in text for word in ("这次", "本次", "朋友", "双方", "两人", "B", "b")):
        return clarify()
    changes, values = {}, {}
    clauses = re.split(r"[，,。；;]", text)
    if not all(clauses):
        return clarify()
    try:
        for clause in clauses:
            if clause in ("其他不变", "其余不变"):
                continue
            mode = re.fullmatch(r"(?:这次|本次)?(?:改为|改成|设为)?(均衡优先|总耗时优先)", clause)
            if mode:
                key, value, roles = "preference", ("balanced" if mode[1] == "均衡优先" else "total_time"), []
            else:
                if scope == "preferences":
                    role = re.match(r"^(我|A|a)", clause)
                    body = clause[role.end():] if role else clause
                    roles = ["person_a"]
                else:
                    role = re.match("^" + ROLE, clause)
                    if not role:
                        return clarify()
                    roles = (["person_a", "person_b"] if role[1] in ("双方", "两人")
                             else ["person_b" if role[1] in ("朋友", "B", "b") else "person_a"])
                    body = clause[role.end():]
                duration = re.fullmatch(LEAD + r"([0-9]+)(分钟|小时)", body)
                transfers = re.fullmatch(r"(?:这次|本次)?(?:仍然|仍|也)?(?:最多|改为|改成)?换乘([0-9]+|[零一二两三四五])次", body)
                walking = re.fullmatch(r"(?:这次|本次)?(?:仍然|仍|也)?(?:最多|改为|改成)?步行([0-9]+)(米|公里)", body)
                origin = re.fullmatch(r"(?:这次|本次)?从(.{1,80})出发", body)
                if duration:
                    key, value = "max_duration_seconds", int(duration[1]) * (60 if duration[2] == "分钟" else 3600)
                elif transfers:
                    key, value = "max_transfers", number(transfers[1])
                elif walking:
                    key, value = "max_walk_meters", int(walking[1]) * (1 if walking[2] == "米" else 1000)
                elif origin and scope == "current":
                    key, value = "origin_text", origin[1]
                else:
                    return clarify()
            if scope == "preferences":
                target = values
                if key in target and target[key] != value:
                    return clarify()
                target[key] = value
            elif key == "preference":
                if key in changes and changes[key] != value:
                    return clarify()
                changes[key] = value
            else:
                for person in roles:
                    target = changes.setdefault(person, {})
                    if key in target and target[key] != value:
                        return clarify()
                    target[key] = value
        if scope == "preferences":
            parsed = PreferenceValues.model_validate(values)
            return {"intent": "preferences", "values": parsed.model_dump(exclude_unset=True)}
        parsed = RequirementsPatch.model_validate(changes)
        return {"intent": "current", "changes": parsed.model_dump(exclude_unset=True)}
    except (ValidationError, ValueError, KeyError):
        return clarify()