#!/usr/bin/env python3
"""V2运行时数据每日备份 — 10:15 cron (2026-08-19新增)
把5个不可重建/难重建的运行时数据快照进 ~/v2_cq_data_backup 并API推送到
github wuya79/v2_cq_data_backup (git push协议被封锁, 走REST API增量推送)。
- 无变化 → 静默退出0 (no_agent空stdout不打扰)
- 永不exit非0 (防看门狗式错误告警; 失败仅打印)
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "wuya79/v2_cq_data_backup"
CWD = os.path.expanduser("~/v2_cq_data_backup")
BRANCH = "main"

# 源文件 → 备份文件名
SOURCES = {
    "/home/ubuntu/v2_cq_strategy/reports/hourly_decisions.csv": "hourly_decisions.csv",
    "/home/ubuntu/v2_cq_strategy/output/cq_price_history.json": "cq_price_history.json",
    "/home/ubuntu/v2_cq_strategy/output/shadow_v2.csv": "shadow_v2.csv",
    "/home/ubuntu/v2_cq_strategy/data/wx_history.json": "wx_history.json",
    "/home/ubuntu/v2_cq_strategy/output/dart_cache.json": "dart_cache.json",
}


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          shell=True, cwd=CWD).stdout.strip()


def api(method, path, data=None):
    tok = open(os.path.expanduser("~/.hermes/keys/GITHUB_TOKEN")).read().strip()
    url = f"https://api.github.com/repos/{REPO}/{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Authorization": f"token {tok}",
                 "Content-Type": "application/json",
                 "User-Agent": "hermes-push/1.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:200]}")


def main():
    os.makedirs(CWD, exist_ok=True)
    if not os.path.exists(os.path.join(CWD, ".git")):
        subprocess.run("git init -b main", shell=True, cwd=CWD, check=True)

    # 1. 快照
    for src, name in SOURCES.items():
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(CWD, name))

    # 2. commit (无变化则静默)
    subprocess.run("git add -A", shell=True, cwd=CWD, check=True)
    diff = sh("git diff --cached --stat")
    if not diff:
        print("无变化, 跳过")  # deliver=local, 不打扰用户
        return
    from datetime import datetime
    msg = f"数据快照 {datetime.now():%Y-%m-%d}"
    subprocess.run(f"git commit -m '{msg}'", shell=True, cwd=CWD, check=True)

    # 3. API增量推送
    base = api("GET", f"git/refs/heads/{BRANCH}")["object"]["sha"]
    bt = api("GET", f"git/commits/{base}")["tree"]["sha"]
    rt = api("GET", f"git/trees/{bt}?recursive=1")["tree"]
    remote = {i["path"]: (i["sha"], i["mode"]) for i in rt if i["type"] == "blob"}
    lt = sh("git rev-parse HEAD^{tree}")
    local = {}
    for line in sh(f"git ls-tree -r {lt}").split("\n"):
        p = line.split(None, 3)
        if len(p) >= 4 and p[1] == "blob":
            local[p[3]] = (p[2], p[0])
    changed = [p for p, (s, m) in local.items()
               if p not in remote or remote[p][0] != s]
    for p in changed:
        raw = subprocess.run(f"git cat-file -p {local[p][0]}",
                             capture_output=True, shell=True, cwd=CWD).stdout
        api("POST", "git/blobs", {"content": base64.b64encode(raw).decode(),
                                  "encoding": "base64"})
    entries = [{"path": p, "mode": m, "type": "blob", "sha": s}
               for p, (s, m) in remote.items() if p not in changed]
    entries += [{"path": p, "mode": m, "type": "blob", "sha": s}
                for p, (s, m) in local.items() if p in changed]
    new_tree = api("POST", "git/trees", {"tree": entries})["sha"]
    an = sh("git log --format=%an -1 HEAD")
    ae = sh("git log --format=%ae -1 HEAD")
    ad = sh("git log --format=%aI -1 HEAD")
    nc = api("POST", "git/commits", {
        "message": sh("git log --format=%B -1 HEAD"), "tree": new_tree,
        "parents": [base],
        "author": {"name": an, "email": ae, "date": ad},
        "committer": {"name": an, "email": ae, "date": ad}})["sha"]
    r = api("PATCH", f"git/refs/heads/{BRANCH}", {"sha": nc, "force": True})
    print(f"✅ 备份已推送 {len(changed)}文件 HEAD={r['object']['sha'][:8]}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"⚠️ 备份失败: {e}")
