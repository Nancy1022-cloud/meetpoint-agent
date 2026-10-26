"""在现有M5工程内执行。仅挂载页面路由并放行m6_live，先检查再备份。"""
import ast
import re
import shutil
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    main_path, store_path = root / "app/main.py", root / "app/m1_store.py"
    required = ["app/m5_api.py", "app/m5_store.py", "app/m5_checkpoint.py",
                "app/m6_models.py", "app/m6_runtime.py", "app/m6_api.py",
                "app/static/meetpoint.html", "app/test_m6.py"]
    if not all((root / name).is_file() for name in required) or not main_path.is_file() or not store_path.is_file():
        raise SystemExit("缺少M5基础文件或M6新文件；未修改原文件。请在原项目根目录核对文件包。")
    original_main = main_path.read_text(encoding="utf-8-sig")
    original_store = store_path.read_text(encoding="utf-8-sig")
    modes = re.search(r'if execution_mode not in \(([^\n]+)\):', original_store)
    if not modes or '"m5_live"' not in modes.group(1) or "app.include_router(m5_router)" not in original_main:
        raise SystemExit("未识别已完成的M5工程；未修改原文件。")
    changed_main, changed_store = original_main, original_store
    if "app.include_router(m6_router)" not in original_main:
        changed_main = original_main.rstrip() + "\n\nfrom app.m6_api import router as m6_router\napp.include_router(m6_router)\n"
    if '"m6_live"' not in modes.group(1):
        changed_store = original_store.replace(modes.group(0), modes.group(0)[:-2] + ', "m6_live"):', 1)
    ast.parse(changed_main)
    ast.parse(changed_store)
    for path in (root / "app").glob("m6*.py"):
        ast.parse(path.read_text(encoding="utf-8-sig"))
    backup = root / "backups/m6_initial"
    for path in (main_path, store_path):
        destination = backup / path.relative_to(root)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    main_path.write_text(changed_main, encoding="utf-8")
    store_path.write_text(changed_store, encoding="utf-8")
    print("M6页面和运行入口已就绪；首次备份位于backups/m6_initial。未修改.env或数据库结构。")


if __name__ == "__main__":
    main()