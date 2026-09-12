#!/usr/bin/env python3
"""全系统关键数据每日备份 — 10:15 cron (2026-08-19 v2: 扩展为全系统)
快照进 ~/v2_cq_data_backup 并按目录组织, REST API推送到 github wuya79/v2_cq_data_backup。
覆盖: v2运行时数据(含模型/归档/replay, 2026-09-11补) / 四川水情历史库 / hermes配置+记忆+skills / 原始数据归档 / 无git小项目代码
- 无变化 → 静默退出0 (deliver=local, 不打扰)
- 永不exit非0 (失败仅打印)
- 2026-09-12修复: ①-ls-tree改-z解析(中文路径被转义bug) ②分块链式建树(>150KB POST必超时)
  ③400/401等瞬时错误也重试 ④响应gzip(树清单563KB→90KB) ⑤单文件失败不拖垮整轮
"""
import base64
import gzip
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
    "/home/ubuntu/v2_cq_strategy/output/35d_settle.csv": "v2/35d_settle.csv",
    "/home/ubuntu/v2_cq_strategy/data/features_enhanced.csv": "v2/features_enhanced.csv",
    "/home/ubuntu/.hermes/metrics/v2_daily.jsonl": "v2/v2_daily_metrics.jsonl",
    "/home/ubuntu/sichuan_hydro_price/.reservoir_history.json": "sichuan/reservoir_history.json",
    "/home/ubuntu/sichuan_hydro_price/.monthly_trade_archive.json": "sichuan/monthly_trade_archive.json",
    "/home/ubuntu/sichuan_hydro_price/.price_history.json": "sichuan/price_history.json",
    "/home/ubuntu/sichuan_hydro_price/.daily_trends.json": "sichuan/daily_trends.json",
    "/home/ubuntu/sichuan_hydro_price/.thermal_trend.json": "sichuan/thermal_trend.json",
    "/home/ubuntu/sichuan_hydro_price/.prediction_quality.json": "sichuan/prediction_quality.json",
    "/home/ubuntu/sichuan_hydro_price/.monthly_cumulative.json": "sichuan/monthly_cumulative.json",
    "/home/ubuntu/sichuan_hydro_price/.weekly_archive.json": "sichuan/weekly_archive.json",
    "/home/ubuntu/sichuan_hydro_price/.weekly_forecast_cache.json": "sichuan/weekly_forecast_cache.json",
    "/home/ubuntu/sichuan_hydro_price/.daily_fc_snapshot.json": "sichuan/daily_fc_snapshot.json",
    "/home/ubuntu/sichuan_hydro_price/.factor_history.json": "sichuan/factor_history.json",
    "/home/ubuntu/sichuan_hydro_price/.weather_cache.json": "sichuan/weather_cache.json",
    "/home/ubuntu/sichuan_hydro_price/.basin_daily_sums.json": "sichuan/basin_daily_sums.json",
    "/home/ubuntu/sichuan_weekly_report/.weekly_snapshots.json": "sichuan/weekly_snapshots.json",
    "/home/ubuntu/.hermes/cron/jobs.json": "hermes/cron_jobs.json",
}

# 目录: 源目录 → 备份相对目录 (rsync增量/整树复制, 排除嵌套.git和锁文件)
DIRS = {
    "/home/ubuntu/.hermes/skills": "hermes/skills",
    "/home/ubuntu/data_archive": "raw/data_archive",
    "/home/ubuntu/sichuan_news_brief": "projects/sichuan_news_brief",
    "/home/ubuntu/sichuan_weather_brief": "projects/sichuan_weather_brief",
    # 2026-09-11 补覆盖(审计P2a): V2模型(含35d系) + 策略归档 + replay(回测镜子)
    "/home/ubuntu/v2_cq_strategy/models": "v2/models",
    "/home/ubuntu/v2_cq_strategy/output/archive": "v2/archive",
    "/home/ubuntu/v2_cq_strategy/output/replay_inputs": "v2/replay_inputs",
    "/home/ubuntu/v2_cq_strategy/output/replay_rebuilt": "v2/replay_rebuilt",
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
                     "Accept-Encoding": "gzip",
                     "User-Agent": "hermes-push/1.0"})
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            raw = resp.read()
            # 2026-09-12修复: 接受gzip响应(树清单563KB→90KB, 减少大下载被截断)
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode())
        except urllib.error.HTTPError as e:
            last = RuntimeError(f"HTTP {e.code}: {e.read().decode()[:200]}")
            # 2026-09-12修复: 400 malformed / 401 Bad credentials / 422(瞬时timeout) 在本线路
            # 为瞬时抖动(同请求重试即过, 实测), 一并退避重试; 5xx继续重试
            if (e.code >= 500 or e.code in (400, 401, 403, 408, 422, 429)) \
                    and i < tries - 1:
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
    # 2026-09-12修复: 原`git ls-tree`文本解析会把非ASCII路径的转义形态
    # (引号+八进制, 如 "\345\256\236...") 原样当路径上传(中文名在远端变乱码条目);
    # 改用 -z (NUL分隔, 永不转义)
    rawz = subprocess.run(["git", "ls-tree", "-r", "-z", lt],
                          capture_output=True, cwd=CWD).stdout
    for ent in rawz.split(b"\0"):
        if not ent:
            continue
        meta, _path = ent.split(b"\t", 1)
        mode, typ, sha = meta.decode().split()
        if typ == "blob":
            local[_path.decode("utf-8")] = (sha, mode)
    changed = [p for p, (s, m) in local.items()
               if p not in remote or remote[p][0] != s]
    # 2026-09-12: 内容一致、仅"远端多出历史遗留文件"时也正打此句(备份永不删远端文件;
    # 该状态长期存在; 供审计守卫判成功)
    if not changed:
        print("远端已是最新, 跳过")
        return
    # 断点续传+并发: 已成功上传的blob记录在.state, 重跑跳过; 8线程并发(串行1s/个太慢)
    state_path = os.path.join(CWD, ".upload_state.json")
    state = {}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path))
        except Exception:
            state = {}
    import threading
    import time as _time
    from concurrent.futures import ThreadPoolExecutor
    _lock = threading.Lock()
    _done = [0]
    _failed = []
    _t0 = _time.time()

    def _up(p):
        sha = local[p][0]
        with _lock:
            if state.get(p) == sha:
                return
        try:
            raw = subprocess.run(f"git cat-file -p {sha}",
                                 capture_output=True, shell=True, cwd=CWD).stdout
            api("POST", "git/blobs", {"content": base64.b64encode(raw).decode(),
                                      "encoding": "base64"})
        except Exception as e:
            # 2026-09-12修复: 单文件失败不拖垮整轮, 记录后下轮补传
            with _lock:
                _failed.append((p, str(e)[:120]))
            return
        with _lock:
            state[p] = sha
            with open(state_path, "w") as _sf:
                json.dump(state, _sf)
            _done[0] += 1
            if _done[0] % 100 == 0:
                print(f"  上传进度 {_done[0]}/{len(changed)} "
                      f"(耗时{_time.time() - _t0:.0f}s)", flush=True)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_up, changed))
    print(f"  blob上传完成 {_done[0]}/{len(changed)} "
          f"耗时{_time.time() - _t0:.0f}s", flush=True)
    if _failed:
        print(f"  ⚠️ {len(_failed)}个文件本轮上传失败(下轮续传): "
              f"{_failed[0][0][:60]} ...", flush=True)
    # 2026-09-12修复: 只提交"已确认上传"的条目, 单文件失败不再阻塞建树提交
    changed_entries = [{"path": p, "mode": m, "type": "blob", "sha": s}
                       for p, (s, m) in local.items()
                       if p in changed and state.get(p) == s]
    if not changed_entries:
        print(f"⚠️ {len(changed)}个变更未确认上传(全失败), 跳过建树提交; 下轮续传", flush=True)
        return
    # 2026-09-12修复: 分块链式建树 — 本线路对>150KB的POST约12s必超时(实测504/502),
    # 全量一次必失败; 每批300条(~66KB, 实测秒过)
    new_tree = bt
    CHUNK = 300
    for _i in range(0, len(changed_entries), CHUNK):
        new_tree = api("POST", "git/trees",
                       {"base_tree": new_tree,
                        "tree": changed_entries[_i:_i + CHUNK]},
                       tries=5)["sha"]
        print(f"  建树进度 {min(_i + CHUNK, len(changed_entries))}/"
              f"{len(changed_entries)}", flush=True)
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
    tail = f" (另有{len(_failed)}个下轮补传)" if _failed else ""
    print(f"✅ 备份已推送 {len(changed_entries)}文件 HEAD={r['object']['sha'][:8]}{tail}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"⚠️ 备份失败: {e}")
