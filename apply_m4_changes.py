"""在现有M3项目内执行；扩展状态白名单及依赖，先检查再备份。"""

import ast
import re
import shutil
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    store_path, requirements_path = root / "app/m1_store.py", root / "requirements.txt"
    original = store_path.read_text(encoding="utf-8-sig")
    dependencies = requirements_path.read_text(encoding="utf-8-sig")
    docker_path = root / "Dockerfile"
    docker_original = docker_path.read_text(encoding="utf-8-sig")
    modes = re.search(r'if execution_mode not in \(([^\n]+)\):', original)
    finishes = re.search(r'        allowed = \{(.*?)\n        \}', original, re.S)
    if not modes or not finishes or '"m3_live"' not in modes.group(1):
        raise SystemExit("未识别到M3状态存储结构；未写入任何文件。请先核对M3指南第2步。")
    changed = original
    extra_modes = [m for m in ("m4_offline", "m4_llm_check", "m4_live") if f'"{m}"' not in modes.group(1)]
    if extra_modes:
        replacement = modes.group(0)[:-2] + ", " + ", ".join(f'"{m}"' for m in extra_modes) + "):"
        changed = changed.replace(modes.group(0), replacement, 1)
    extra_reasons = [r for r in ("agent_finished", "no_legal_actions", "model_output_invalid", "llm_error",
                                  "budget_exhausted:llm_calls") if f'"{r}"' not in finishes.group(1)]
    if extra_reasons:
        content = finishes.group(1).rstrip()
        if not content.endswith(","):
            content += ","
        content += "\n            " + ", ".join(f'"{r}"' for r in extra_reasons) + ","
        changed = changed.replace(finishes.group(0), "        allowed = {" + content + "\n        }", 1)
    ast.parse(changed)
    pins = re.findall(r"^langgraph\s*[^\n]*", dependencies, re.M)
    if pins and pins != ["langgraph==1.0.5"]:
        raise SystemExit("requirements.txt已有其他LangGraph版本；未写入任何文件。请核对依赖。")
    updated_dependencies = dependencies if pins else dependencies.rstrip() + "\nlanggraph==1.0.5\n"
    constraint_line = "-c requirements-m4-constraints.txt"
    if constraint_line not in updated_dependencies.splitlines():
        updated_dependencies = updated_dependencies.rstrip() + "\n" + constraint_line + "\n"
    if not (root / "requirements-m4-constraints.txt").is_file():
        raise SystemExit("缺少requirements-m4-constraints.txt；未写入任何文件。请完整复制M4文件包。")
    docker_copy = "COPY requirements.txt requirements-m4-constraints.txt ./"
    if docker_copy in docker_original:
        docker_updated = docker_original
    else:
        docker_updated, count = re.subn(r"(?m)^COPY requirements\.txt \./?\s*$", docker_copy,
                                         docker_original, count=1)
        if count != 1:
            raise SystemExit("未识别Dockerfile依赖复制行；未写入任何文件。请按指南手动修改。")
    backup = root / "backups/m4_initial"
    for path in (store_path, requirements_path, docker_path):
        destination = backup / path.relative_to(root)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    store_path.write_text(changed, encoding="utf-8")
    requirements_path.write_text(updated_dependencies, encoding="utf-8")
    docker_path.write_text(docker_updated, encoding="utf-8")
    print("M4状态白名单、依赖和Docker复制规则已就绪；首次备份位于backups/m4_initial。")


if __name__ == "__main__":
    main()
