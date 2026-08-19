#!/usr/bin/env python3
"""全系统关键数据每日备份 — 10:15 cron (2026-08-19 v2: 扩展为全系统)
快照进 ~/v2_cq_data_backup 并按目录组织, REST API推送到 github wuya79/v2_cq_data_backup。
覆盖: v2运行时数据 / 四川水情历史库 / hermes配置+记忆+skills / 原始数据归档 / 无git小项目代码
- 无变化 → 静默退出0 (deliver=local, 不打扰)
- 永不exit非0 (失败仅打印)
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
RSYNC = shutil.which("rsync")

# 单文件: 源路径 → 备份相对路径
FILES = {
    "/home/ubuntu/v2_cq_strategy/reports/hourly_decisions.csv": "v2/hourly_decisions.csv",
    "/home/ubuntu/v2_cq_strategy/output/cq_price_history.json": "v2/cq_price_history.json",
    "/home/ubuntu/v2_cq_strategy/output/shadow_v2.csv": "v2/shadow_v2.csv",
    "/home/ubuntu/v2_cq_strategy/data/wx_history.json": "v2/wx_history.json",
    "/home/ubuntu/v2_cq_strategy/output/dart_cache.json": "v2/dart_cache.json",
    "/home/ubuntu/.hermes/metrics/v2_daily.jsonl": "v2/v2_daily_metrics.jsonl",
    "/home/ubuntu/sichuan_hydro_price/.reservoir_history.json": "sichuan/reservoir_history.json",
    "/home/ubuntu/sichuan_hydro_price/.monthly_trade_archive.json": "sichuan/monthly_trade_archive.json",
    "/home/ubuntu/.hermes/cron/jobs.json": "hermes/cron_jobs.json",
}

# 目录: 源目录 → 备份相对目录 (rsync增量/整树复制, 排除嵌套.git和锁文件)
DIRS = {
    "/home/ubuntu/.hermes/skills": "hermes/skills",
    "/home/ubuntu/data_archive": "raw/data_archive",
    "/home/ubuntu/sichuan_news_brief": "projects/sichuan_news_brief",
    "/home/ubuntu/sichuan_weather_brief": "projects/sichuan_weather_brief",
}
RSYNC_EXCLUDES = ["--exclude=.git/", "--exclude=*.lock", "--exclude=__pycache__/",
                  "--exclude=*.pyc", "--exclude=cache/", "--exclude=*.png",
                  "--exclude=.curator_backups/"]


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          shell=True, cwd=CWD).stdout.strip()


def api(method, path, data=None, tries=3):
    import time
    tok = open(os.path.expanduser("~/.hermes/keys/GITHUB_TOKEN")).read().strip()
    url = f"https://api.github.com/repos/{REPO}/{path}"
    body = json.dumps(data).encode() if data is not None else None
    last = None
    for i in range(tries):
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"Authorization": f"token {tok}",
                     "Content-Type": "application/json",
                     "User-Agent": "hermes-push/1.0"})
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last = RuntimeError(f"HTTP {e.code}: {e.read().decode()[:200]}")
            if e.code >= 500 and i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise last
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise last
    raise last if last is not None else RuntimeError("unknown")


def main():
    os.makedirs(CWD, exist_ok=True)
    if not os.path.exists(os.path.join(CWD, ".git")):
        subprocess.run("git init -b main", shell=True, cwd=CWD, check=True)

    # 1. 单文件快照
    for src, rel in FILES.items():
        if os.path.exists(src):
            dst = os.path.join(CWD, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

    # 2. 目录快照
    for src, rel in DIRS.items():
        if not os.path.isdir(src):
            continue
        dst = os.path.join(CWD, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if RSYNC:
            # 注意: 不带--delete, 备份永不删除任何文件(源删了副本也保留=备份的意义)
            subprocess.run(["rsync", "-a"] + RSYNC_EXCLUDES
                           + [src.rstrip("/") + "/", dst + "/"],
                           check=False)
        else:
            subprocess.run(["cp", "-r", src.rstrip("/") + "/.", dst],
                           check=False)

    # 3. commit (本地快照, 无变化不commit)
    subprocess.run("git add -A", shell=True, cwd=CWD, check=True)
    diff = sh("git diff --cached --stat")
    if diff:
        from datetime import datetime
        msg = f"全系统数据快照 {datetime.now():%Y-%m-%d}"
        subprocess.run(f"git commit -m '{msg}'", shell=True, cwd=CWD, check=True)

    # 4. 推送本地HEAD树到远端(对比远端树, 只传缺的blob; 相同则跳过)
    #    不依赖"本次是否新commit"——上次push失败后也能靠这步补推
    base = api("GET", f"git/refs/heads/{BRANCH}")["object"]["sha"]
    bt = api("GET", f"git/commits/{base}")["tree"]["sha"]
    lt = sh("git rev-parse HEAD^{tree}")
    if bt == lt:
        print("远端已是最新, 跳过")
        return
    rt = api("GET", f"git/trees/{bt}?recursive=1")["tree"]
    remote = {i["path"]: (i["sha"], i["mode"]) for i in rt if i["type"] == "blob"}
    local = {}
    for line in sh(f"git ls-tree -r {lt}").split("\n"):
        p = line.split(None, 3)
        if len(p) >= 4 and p[1] == "blob":
            local[p[3]] = (p[2], p[0])
    changed = [p for p, (s, m) in local.items()
               if p not in remote or remote[p][0] != s]
    # 断点续传: 已成功上传的blob记录在.state, 重跑时跳过(GitHub API不稳定时的收敛机制)
    state_path = os.path.join(CWD, ".upload_state.json")
    state = {}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path))
        except Exception:
            state = {}
    for i, p in enumerate(changed):
        sha = local[p][0]
        if state.get(p) == sha:
            continue  # 已上传过, 跳过
        raw = subprocess.run(f"git cat-file -p {sha}",
                             capture_output=True, shell=True, cwd=CWD).stdout
        api("POST", "git/blobs", {"content": base64.b64encode(raw).decode(),
                                  "encoding": "base64"})
        state[p] = sha
        with open(state_path, "w") as _sf:
            json.dump(state, _sf)
        if (i + 1) % 50 == 0:
            print(f"  上传进度 {i + 1}/{len(changed)}")
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
    if os.path.exists(state_path):
        os.remove(state_path)  # 本轮完整成功, 清空断点状态
    print(f"✅ 备份已推送 {len(changed)}文件 HEAD={r['object']['sha'][:8]}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"⚠️ 备份失败: {e}")
