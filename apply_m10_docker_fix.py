"""给现有M10工程补齐两个只读挂载；仅用标准库，可通过原app镜像执行。"""

from datetime import datetime
import json
from pathlib import Path
import re
import shutil


MOUNTS = (
    "./apply_m10_changes.py:/app/apply_m10_changes.py:ro",
    "./M10_patch_manifest.json:/app/M10_patch_manifest.json:ro",
)


def apply(root):
    root = Path(root)
    for name in ("apply_m10_changes.py", "M10_patch_manifest.json", "compose.override.yaml"):
        if not (root / name).is_file():
            raise ValueError("缺少文件：" + name + "；先保留现场，核对M10代码包的复制步骤。")
    path = root / "compose.override.yaml"
    original = path.read_text(encoding="utf-8-sig")
    # 只处理已经核对的原app数据挂载；不重写用户的environment、ports等配置。
    anchor = re.compile(r"(?m)^(?P<indent> +)-\s+\./data:/app/data\s*$")
    matches = list(anchor.finditer(original))
    if len(matches) != 1:
        raise ValueError("未找到唯一的原 ./data:/app/data 挂载行；配置未修改，请提供compose.override.yaml核对。")
    levels = {}
    for line in original[:matches[0].start()].splitlines():
        header = re.match(r"^( *)([A-Za-z_][\w-]*):", line)
        if header:
            levels[len(header.group(1))] = header.group(2)
    if ({depth: levels.get(depth) for depth in (0, 2, 4)} != {0: "services", 2: "app", 4: "volumes"}
            or matches[0].group("indent") != "      "):
        raise ValueError("数据挂载不在已核对的services.app.volumes结构内；配置未修改，请先核对。")
    missing = []
    for mount in MOUNTS:
        target = mount.split(":", 2)[1]
        lines = [line.strip() for line in original.splitlines() if target in line and not line.lstrip().startswith("#")]
        expected = "- " + mount
        if lines and lines != [expected]:
            raise ValueError("发现不同的已有挂载：" + target + "；配置未修改，请先核对。")
        if not lines:
            missing.append(mount)
    if not missing:
        print(json.dumps({"status": "already_ready", "changed": False, "mounts": list(MOUNTS)}, ensure_ascii=False))
        return
    match = matches[0]
    # 排除正则末尾\s*可能吞掉的换行，以一行作为替换范围。
    start, end = match.start(), original.find("\n", match.start())
    if end < 0:
        end = len(original)
    line = original[start:end].rstrip("\r")
    insertion = line + "\n" + "\n".join(match.group("indent") + "- " + value for value in missing)
    changed = (original[:start] + insertion + original[end:]).replace("\r\n", "\n")
    if not changed.endswith("\n"):
        changed += "\n"
    backup = root / "backups" / ("m10_docker_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")) / path.name
    backup.parent.mkdir(parents=True, exist_ok=False)
    shutil.copy2(path, backup)
    temp = path.with_name(path.name + ".m10.tmp")
    temp.write_text(changed, encoding="utf-8", newline="\n")
    temp.replace(path)
    print(json.dumps({"status": "patched", "changed": True, "file": path.name,
                      "backup": str(backup), "added_read_only_mounts": missing}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        apply(Path(__file__).resolve().parent)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc))
