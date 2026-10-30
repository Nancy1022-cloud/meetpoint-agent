"""Python standard library installer. Preflight, backup, patch, reversible integration."""
import argparse
import ast
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil

MARKER="M11_INTEGRATION_V1"
def sha(text):return hashlib.sha256(text.encode("utf-8")).hexdigest()
def read(path):return path.read_text(encoding="utf-8-sig")

def apply(root,rollback=False):
    root=Path(root).resolve();record=root/"data/m11/v1/install.json"
    if rollback:
        if not record.exists():raise ValueError("没有本安装器的记录，不推测回退内容。")
        data=json.loads(read(record));planned=[]
        for name,spec in data["files"].items():
            path=root/name;actual=sha(read(path))
            if actual==spec["before"]:continue
            if actual!=spec["after"]:raise ValueError(name+"在安装后已变化，拒绝覆盖。")
            planned.append((name,path,(root/data["backup"]/name).read_bytes()))
        for name,path,raw in planned:path.write_bytes(raw)
        print(json.dumps({"rollback":True,"changed":[n for n,_,_ in planned]},ensure_ascii=False));return
    manifest=json.loads(read(root/"M11_patch_manifest.json"));planned=[]
    runtime=read(root/"app/m6_runtime.py")
    if "M10_INTEGRATION_V1" not in runtime or "M9_INTEGRATION_V1" not in runtime:
        raise ValueError("先恢复M10网页运行器并通过doctor，再接入M11。")
    for name,spec in manifest["files"].items():
        path=root/name;text=read(path)
        if sha(text)==spec["after"]:continue
        if sha(text)!=spec["before"]:raise ValueError(name+"与已核对M10源码不一致；保留原件，不覆盖。")
        target=text
        for rep in spec["replacements"]:
            if target.count(rep["old"])!=1:raise ValueError("接入锚点不唯一："+name)
            target=target.replace(rep["old"],rep["new"])
        if sha(target)!=spec["after"]:raise ValueError("目标哈希不匹配："+name)
        if name.endswith(".py"):ast.parse(target)
        planned.append((name,path,target))
    path=root/"app/main.py";text=read(path)
    if MARKER not in text:
        tree=ast.parse(text)
        if not any(isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="app" for t in n.targets) for n in tree.body):
            raise ValueError("main.py没有可识别的app实例，请保留源码供核对。")
        if "m11_api" in text:raise ValueError("main.py已有未识别的M11接入，请先核对。")
        target=text.rstrip()+"\n\n# M11_INTEGRATION_V1\nfrom app.m11_api import router as m11_router\napp.include_router(m11_router)\n"
        ast.parse(target);planned.append(("app/main.py",path,target))
    if not planned:
        print(json.dumps({"changed":[],"already_installed":True}));return
    # Refuse a partial/altered installation rather than overwriting its rollback record.
    if record.exists():
        old=json.loads(read(record))
        for name,spec in old["files"].items():
            actual=sha(read(root/name))
            if actual not in (spec["before"],spec["after"]):raise ValueError("已有安装记录与当前源码冲突。")
    backup=Path("backups")/("m11_"+datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    journal={"backup":backup.as_posix(),"files":{}}
    for name,path,target in planned:
        dest=root/backup/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
        journal["files"][name]={"before":sha(read(path)),"after":sha(target)}
    done=[]
    try:
        for name,path,target in planned:
            temp=path.with_name(path.name+".m11.tmp");temp.write_text(target,encoding="utf-8",newline="\n");temp.replace(path);done.append((name,path))
        record.parent.mkdir(parents=True,exist_ok=True);record.write_text(json.dumps(journal,ensure_ascii=False,indent=2),encoding="utf-8")
    except Exception:
        for name,path in done:shutil.copy2(root/backup/name,path)
        raise
    print(json.dumps({"changed":[n for n,_,_ in planned],"backup":backup.as_posix(),"rollback":False},ensure_ascii=False))

if __name__=="__main__":
    ap=argparse.ArgumentParser();ap.add_argument("--root",default=str(Path(__file__).resolve().parent));ap.add_argument("--rollback",action="store_true")
    args=ap.parse_args()
    try:apply(args.root,args.rollback)
    except (ValueError,FileNotFoundError) as exc:raise SystemExit(str(exc))
