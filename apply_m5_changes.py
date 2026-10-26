"""在已验收的M4工程执行；检查完成才写文件，备份原入口与配置。"""

import ast
import re
import shutil
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    paths = [root / name for name in ("app/main.py", "app/m1_store.py", "requirements.txt")]
    if not all(path.is_file() for path in paths) or not (root / "app/m4_agent.py").is_file():
        raise SystemExit("未找到现有M4工程；未写入文件。请在原项目根目录执行。")
    main_text, store_text, dependencies = [p.read_text(encoding="utf-8-sig") for p in paths]
    if '"m4_live"' not in store_text or "checkpointer=False" not in (root / "app/m4_agent.py").read_text(encoding="utf-8-sig"):
        raise SystemExit("未识别到M4的运行模式或安全状态边界；未写入文件。")
    if not all((root / "app" / name).is_file() for name in (
        "m5_models.py", "m5_parse.py", "m5_store.py", "m5_api.py", "m5_checkpoint.py", "m5.py", "test_m5.py",
    )):
        raise SystemExit("M5文件不完整；未写入文件。请完整复制app中的7个新文件。")
    modes = re.search(r'if execution_mode not in \(([^\n]+)\):', store_text)
    if not modes:
        raise SystemExit("未识别运行模式白名单；未写入文件。")
    if '"m5_live"' not in modes.group(1):
        replacement = modes.group(0)[:-2] + ', "m5_live"):'
        store_text = store_text.replace(modes.group(0), replacement, 1)
    if "from app.m5_api import router as m5_router" not in main_text:
        marker = "app.include_router(m1_router)"
        if marker not in main_text:
            raise SystemExit("未识别现有M1路由注册位置；未写入文件。")
        main_text = main_text.replace(marker, marker + "\n\nfrom app.m5_api import router as m5_router\napp.include_router(m5_router)", 1)
    pins = {"langgraph-checkpoint-sqlite": "3.0.1", "aiosqlite": "0.22.1", "sqlite-vec": "0.1.9"}
    for name, version in pins.items():
        matches = re.findall(r"(?m)^" + re.escape(name) + r"\s*[^\n]*", dependencies)
        wanted = name + "==" + version
        if matches and matches != [wanted]:
            raise SystemExit(f"{name}已有其他约束；未写入文件，请先核对。")
        if not matches:
            dependencies = dependencies.rstrip() + "\n" + wanted + "\n"
    ast.parse(main_text)
    ast.parse(store_text)
    for path in (root / "app").glob("m5*.py"):
        ast.parse(path.read_text(encoding="utf-8-sig"))
    backup = root / "backups/m5_initial"
    for path in paths:
        dest = backup / path.relative_to(root)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    for path, content in zip(paths, (main_text, store_text, dependencies)):
        path.write_text(content, encoding="utf-8")
    print("M5路由、运行模式和检查点依赖已就绪；原文件位于backups/m5_initial。")


if __name__ == "__main__":
    main()