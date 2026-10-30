"""在Windows主机执行：核对M9、备份、接入M10；--rollback仅回退本脚本的两处改动。"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply(root, rollback=False):
    root = Path(root)
    manifest = json.loads((root / "M10_patch_manifest.json").read_text(encoding="utf-8-sig"))
    planned = []
    for name, spec in manifest["files"].items():
        path = root / name
        original = path.read_text(encoding="utf-8-sig")
        if "M9_INTEGRATION_V1" not in original:
            raise ValueError(name + " 尚未接入M9")
        expected, target = (spec["after"], spec["before"]) if rollback else (spec["before"], spec["after"])
        current = sha(original)
        if current == target:
            continue
        if current != expected:
            raise ValueError(name + " 与已核对源码不同；保留原件供核对，不覆盖或重置工程")
        changed = original
        for rep in spec["replacements"]:
            left, right = (rep["new"], rep["old"]) if rollback else (rep["old"], rep["new"])
            if changed.count(left) != 1:
                raise ValueError("接入锚点不唯一：" + name)
            changed = changed.replace(left, right)
        if sha(changed) != target:
            raise ValueError("接入结果校验失败：" + name)
        planned.append((name, path, changed))
    if not planned:
        print("已回退至M9，无需重复修改" if rollback else "已接入M10，无需重复修改")
        return
    backup = root / "backups" / ("m10_rollback_" if rollback else "m10_")
    backup = backup.with_name(backup.name + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    for name, path, changed in planned:
        dest = backup / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        temp = path.with_name(path.name + ".m10.tmp")
        temp.write_text(changed, encoding="utf-8", newline="\n")
        temp.replace(path)
    print(json.dumps({"changed": [n for n, _, _ in planned], "backup": str(backup), "rollback": rollback}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    try:
        apply(Path(__file__).resolve().parent, args.rollback)
    except (ValueError, FileNotFoundError) as exc:
        raise SystemExit(str(exc))
