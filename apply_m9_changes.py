"""在Windows原工程根目录运行：先核对、备份，再接入M9；不会处理.env或数据库。"""

import ast
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil

MARKER = "M9_INTEGRATION_V1"


def once(text, before, after):
    if text.count(before) != 1:
        raise ValueError("未识别唯一修改位置：" + before[:100])
    return text.replace(before, after, 1)


def transform(path, source, root):
    if MARKER in source:
        return source
    if path == "app/m5_api.py":
        source = once(source, "from app.m5_store import MemoryStore", "from app.m5_store import MemoryStore\nfrom app.m9_scope import apply_scoped_message  # " + MARKER)
        return once(source, '        return getattr(MemoryStore(), method)(*args)',
            '        store = MemoryStore()\n        if method == "apply_message":\n            return apply_scoped_message(store, *args)\n        return getattr(store, method)(*args)')
    if path == "app/m6_runtime.py":
        source = once(source, "from app.m5_store import MemoryStore", "from app.m5_store import MemoryStore\nfrom app.m9_evidence import EvidenceTools, web_view  # " + MARKER)
        source = once(source, '            with self.resources_factory(store, job.request, rid) as (client, decider, tools):',
            '            with self.resources_factory(store, job.request, rid) as (client, decider, tools):\n                observer = EvidenceTools(tools, rid, job.run["constraints_version"])')
        source = once(source, 'planner = WebPlanner(tools, job.task, decider, saver,', 'planner = WebPlanner(observer, job.task, decider, saver,')
        return once(source, '                    result = planner.summary(finished["stop_reason"])',
            '                    result = planner.summary(finished["stop_reason"])\n'
            '                    result["m9_presentation"] = web_view(observer, result, job.task["requirements"], finished["stop_reason"])\n'
            '                    if not result["m9_presentation"]["evidence_valid"]:\n'
            '                        result["recommendations"] = []\n'
            '                        status, error = "failed", {"code": "evidence_validation_failed", "message": "推荐证据未通过校验。"}')
    if path == "app/static/meetpoint.html":
        source = once(source, "paintMemory(d);if(d.status==='needs_clarification')chat(d.question);",
            "paintMemory(d);if(d.status==='refused')chat(d.message+' '+d.capabilities);else if(d.status==='needs_clarification')chat(d.question);")
        source = once(source, "banner(d.status==='needs_clarification'?'请明确新要求后再规划。':'更新完成。');",
            "banner(d.status==='refused'?'该请求超出支持范围。':d.status==='needs_clarification'?'请明确新要求后再规划。':'更新完成。');")
        source, count = re.subn(r"const cards=\$\('cards'\);cards\.replaceChildren\(\);for\(const \[index,item\].*?(?=\n\$\('checks'\))",
                               "paintM9Cards(d);", source, flags=re.S)
        if count != 1:
            raise ValueError("未识别原页面推荐卡片块，未写入任何修改")
        return once(source, "function paintJob(d)", (root / "app/static/m9_ui.js").read_text(encoding="utf-8-sig") + "\nfunction paintJob(d)")
    raise ValueError("未知目标：" + path)


def main():
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "M9_patch_manifest.json").read_text(encoding="utf-8-sig"))
    for name in ("app/m8.py", "app/m8_metrics.py", "app/m9_scope.py", "app/m9_evidence.py", "app/m9.py", "app/test_m9.py", "app/static/m9_ui.js"):
        if not (root / name).is_file():
            raise SystemExit("缺少文件：" + name + "；请先完成M8并复制M9代码包。")
    originals, changes = {}, {}
    try:
        for name, expected in manifest["original_normalized_sha256"].items():
            source = (root / name).read_text(encoding="utf-8-sig")
            if MARKER not in source and hashlib.sha256(source.encode()).hexdigest() != expected:
                raise ValueError(name + "与核对基线不同；为保留本地改动，本次未自动修改。请提供该文件核对，不要删库或重置工程。")
            originals[name] = source
            changes[name] = transform(name, source, root)
            if name.endswith(".py"):
                ast.parse(changes[name])
        for path in (root / "app").glob("m9*.py"):
            ast.parse(path.read_text(encoding="utf-8-sig"))
    except (ValueError, SyntaxError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None
    touched = [name for name in changes if changes[name] != originals[name]]
    if not touched:
        print("M9已接入，无需重复修改。")
        return
    backup = root / "backups" / ("m9_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    for name in touched:
        target = backup / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target)
    for name in touched:
        (root / name).write_text(changes[name], encoding="utf-8", newline="\n")
    print("M9已接入，修改文件：" + ", ".join(touched))
    print("原文件备份：" + str(backup.relative_to(root)))


if __name__ == "__main__":
    main()
