#!/usr/bin/env python3
"""微信投递保障网 (wx_delivery_guard)

检测 iLink 限流吞掉的【最终回复】→ 自动经个人企微单聊补发原文。
- 由 cron 每 5 分钟静默运行（stdout 保持空）；干跑见 --dry-run 用法。
- 证据链：gateway.log 中 "[Weixin] Sending response (N chars) to <user>" 后
  跟 "send failed to=<user>" 即判定该条被吞；内容从 state.db messages 表
  按 时间窗口 + 长度 匹配取回。
- 状态: ~/.hermes/scripts/.wx_guard_state.json（last_ts / handled / pending）
- 日志: ~/.hermes/logs/wx_delivery_guard.log

干跑:  wx_delivery_guard.py --dry-run --since "2026-09-14 17:00:00"
"""
import os
import re
import sys
import json
import time
import sqlite3
import asyncio
import datetime
import subprocess

HOME = os.path.expanduser("~")
GW_LOG = f"{HOME}/.hermes/logs/gateway.log"
STATE = f"{HOME}/.hermes/scripts/.wx_guard_state.json"
GUARD_LOG = f"{HOME}/.hermes/logs/wx_delivery_guard.log"
DB = f"{HOME}/.hermes/state.db"
WX_TARGET = "o9cq80wz"          # 微信侧收件人（本机唯一 DM）
WECOM_DM = "QiuLing"            # 个人企微单聊（userid 即 chatid）
MATCH_BACK = 240                # 秒：向前找 Sending response 的窗口
RESEND_MAX_ATTEMPTS = 3
TAIL_LINES = 12000

# ---- cron 投递失败 → 个人企微兜底（2026-11-16 新增）----
CRON_JOBS = f"{HOME}/.hermes/cron/jobs.json"
CRON_OUT_DIR = f"{HOME}/.hermes/cron/output"
CRON_MAX_AGE = 8 * 3600         # 秒：只看 8 小时内跑过的 job，避免回放陈年失败
CRON_MAX_CHARS = 3800           # 企微单条上限 4000，留余量
# 只补业务类 job；技术探针（OSS探测等）不补
CRON_WHITELIST = {
    "55e76a6c0f42": "四川水电电价日报",
    "c9575c5b6af8": "V2策略日报",
    "9334c20fb78d": "V2单阶段策略(重庆)",
    "6714b3433f9a": "发电侧日报PDF",
    "21a847f3e6c4": "售电侧日报PDF链接",
    "09d688a65c23": "新闻早报",
    "a8bddbae38d2": "新闻午报",
    "ccbaaeeecbd2": "交易天气简报",
}
# 已自行直发个人企微的 job（看门狗），guard 不再重复补
CRON_SKIP = {"000a49662763"}


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%F %T")
    with open(GUARD_LOG, "a", encoding="utf-8") as f:
        f.write(f"{ts} | {msg}\n")


def parse_ts(s: str) -> float:
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f").timestamp()


def load_state() -> dict:
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8"))
        except Exception:
            pass
    return {"last_ts": 0.0, "handled": [], "pending": [], "cron_handled": []}


def save_state(st: dict) -> None:
    st["handled"] = st["handled"][-500:]
    # cron_handled 可能与看门狗并发写：落盘前与磁盘做并集，避免互相覆盖
    try:
        disk = json.load(open(STATE, encoding="utf-8"))
        merged = list(dict.fromkeys((st.get("cron_handled") or []) + (disk.get("cron_handled") or [])))
    except Exception:
        merged = st.get("cron_handled") or []
    st["cron_handled"] = merged[-500:]
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def scan_events(since_ts: float) -> list:
    out = subprocess.run(
        ["tail", "-n", str(TAIL_LINES), GW_LOG],
        capture_output=True, text=True, errors="replace",
    ).stdout.splitlines()
    events = []
    for ln in out:
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})", ln)
        if not m:
            continue
        ts = parse_ts(m.group(1))
        if ts <= since_ts:
            continue
        if "[Weixin] Sending response" in ln and WX_TARGET in ln:
            n = re.search(r"\((\d+) chars\)", ln)
            events.append({"ts": ts, "type": "resp",
                           "chars": int(n.group(1)) if n else None})
        elif f"send failed to={WX_TARGET}" in ln:
            events.append({"ts": ts, "type": "fail"})
    events.sort(key=lambda e: e["ts"])
    return events


def find_losses(events: list) -> list:
    losses, last_resp = [], None
    for e in events:
        if e["type"] == "resp":
            last_resp = e
        elif e["type"] == "fail" and last_resp is not None:
            if e["ts"] - last_resp["ts"] <= MATCH_BACK:
                losses.append({
                    "key": f"{int(e['ts'])}-{last_resp.get('chars')}",
                    "fail_ts": e["ts"],
                    "resp_ts": last_resp["ts"],
                    "chars": last_resp.get("chars"),
                })
            last_resp = None
    return losses


def recover_content(fail_ts: float, chars):
    """从 messages 表按 [失败前10分钟, 失败后60秒] + 长度 匹配原文。"""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT m.content, m.timestamp, m.session_id FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.role='assistant'
              AND m.timestamp >= ? AND m.timestamp <= ?
              AND s.chat_id LIKE ?
            ORDER BY m.timestamp DESC LIMIT 20
            """,
            (fail_ts - 600, fail_ts + 60, f"%{WX_TARGET}%"),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return None, "no-rows"
    if chars:
        for c, _t, s in rows:
            if c and abs(len(c) - chars) <= 2:
                return c, f"exact:{s}"
    c, _t, s = rows[0]
    return (c, f"latest:{s}") if c else (None, "empty")


def cron_key(job_id: str, run_iso: str) -> str:
    try:
        t = datetime.datetime.fromisoformat(run_iso)
        return f"cron-{job_id}-{t.strftime('%Y%m%d_%H%M%S')}"
    except Exception:
        return f"cron-{job_id}-{run_iso[:19]}"


def cron_find_content(job_id: str, run_dt: datetime.datetime):
    """从 cron/output/<job_id>/ 取该次运行的正文，剥掉 job 头部元信息。"""
    d = os.path.join(CRON_OUT_DIR, job_id)
    if not os.path.isdir(d):
        return None, "no-dir"
    cands = []
    for fn in os.listdir(d):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.md$", fn)
        if not m:
            continue
        try:
            ft = datetime.datetime.strptime(
                f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}", "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        cands.append((ft, os.path.join(d, fn)))
    if not cands:
        return None, "no-files"
    cands.sort()
    deadline = run_dt.astimezone().replace(tzinfo=None) + datetime.timedelta(minutes=5)
    picked = None
    for ft, path in cands:
        if ft <= deadline:
            picked = path
    if picked is None:
        picked = cands[-1][1]
    try:
        txt = open(picked, encoding="utf-8").read()
    except Exception:
        return None, "unreadable"
    # agent 型 job 的文件里塞了 Prompt（甚至整个 skill），只取 Response 之后
    if "\n## Response\n" in txt:
        txt = txt.split("\n## Response\n")[-1]
    elif "\n---\n" in txt:
        txt = txt.split("\n---\n", 1)[1]
    txt = txt.strip()
    if not txt or txt.lower().startswith("status: silent"):
        return None, "empty"
    if len(txt) > CRON_MAX_CHARS:
        txt = txt[:CRON_MAX_CHARS] + "\n…（正文过长已截断）"
    return txt, os.path.basename(picked)


def scan_cron_failures(st: dict) -> list:
    """jobs.json 里 last_delivery_error 非空 且 本次运行在 8h 内 → 待补。"""
    if not os.path.exists(CRON_JOBS):
        return []
    try:
        data = json.load(open(CRON_JOBS, encoding="utf-8"))
    except Exception:
        return []
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    handled = set(st.get("cron_handled") or [])
    out, now = [], time.time()
    for j in jobs or []:
        jid = j.get("id")
        if not jid or jid not in CRON_WHITELIST or jid in CRON_SKIP:
            continue
        err, run_iso = j.get("last_delivery_error"), j.get("last_run_at") or ""
        if not err or not run_iso:
            continue
        try:
            run_dt = datetime.datetime.fromisoformat(run_iso)
        except Exception:
            continue
        if now - run_dt.timestamp() > CRON_MAX_AGE:
            continue
        key = cron_key(jid, run_iso)
        if key in handled:
            continue
        out.append({"key": key, "job_id": jid, "label": CRON_WHITELIST[jid],
                    "run_dt": run_dt, "deliver": str(j.get("deliver") or ""),
                    "err": str(err)[:100]})
    return out


def load_wecom_env() -> None:
    for line in open(f"{HOME}/.hermes/.env", encoding="utf-8"):
        line = line.strip()
        if line.startswith("WECOM_") and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


def wecom_resend(text: str):
    sys.path.insert(0, f"{HOME}/.hermes/hermes-agent")

    async def run():
        from gateway.config import PlatformConfig
        from plugins.platforms.wecom.adapter import _standalone_send
        pconfig = PlatformConfig(
            enabled=True,
            extra={"bot_id": os.environ.get("WECOM_BOT_ID", ""),
                   "secret": os.environ.get("WECOM_SECRET", "")},
        )
        return await _standalone_send(pconfig, WECOM_DM, text)

    return asyncio.run(run())


def main() -> int:
    dry = "--dry-run" in sys.argv
    seed = "--seed" in sys.argv      # 只把当前已失败项标记为已处理，不发送（上线首跑用）
    st = load_state()
    if dry:
        if "--since" in sys.argv:
            since = parse_ts(sys.argv[sys.argv.index("--since") + 1] + ",000")
        else:
            since = time.time() - 86400
    else:
        # 2026-10-16 修复：扫描窗回溯重叠 300s——防"resp 被上一轮消费、fail 落下一轮"的跨轮漏检（补发去重靠 handled 键，跨轮稳定）
        since = (st.get("last_ts", 0.0) or (time.time() - 300)) - 300

    events = scan_events(since)
    max_ts = max((e["ts"] for e in events), default=since)
    losses = [l for l in find_losses(events) if l["key"] not in st.get("handled", [])]
    todo = losses + [p for p in st.get("pending", []) if p["key"] not in st.get("handled", [])]

    for item in todo:
        content, how = recover_content(item["fail_ts"], item.get("chars"))
        if not content:
            log(f"LOSS {item['key']} -> content NOT FOUND ({how})")
            if not dry:
                st.setdefault("handled", []).append(item["key"])
            continue
        if dry:
            print(f"WOULD RESEND key={item['key']} chars={item.get('chars')} "
                  f"how={how} preview={content[:80]!r}")
            continue
        text = ("⚠️【自动补发】检测到微信通道刚才吞了一条回复（限流），"
                "本条由投递保障网自动经个人企微补上，原文如下：\n\n" + content)
        try:
            load_wecom_env()
            res = wecom_resend(text)
        except Exception as e:  # noqa: BLE001
            res = {"exception": repr(e)}
        ok = isinstance(res, dict) and res.get("success")
        log(f"LOSS {item['key']} chars={item.get('chars')} how={how} "
            f"resend={'OK' if ok else res}")
        if ok:
            st.setdefault("handled", []).append(item["key"])
            st["pending"] = [p for p in st.get("pending", []) if p["key"] != item["key"]]
        else:
            item["attempts"] = item.get("attempts", 0) + 1
            if item["attempts"] >= RESEND_MAX_ATTEMPTS:
                st.setdefault("handled", []).append(item["key"])
                log(f"LOSS {item['key']} gave up after {item['attempts']} attempts")
            elif not any(p["key"] == item["key"] for p in st.get("pending", [])):
                st.setdefault("pending", []).append(item)

    # ---- cron 投递失败 → 个人企微兜底（业务 job 白名单）----
    for item in scan_cron_failures(st):
        if seed:
            st.setdefault("cron_handled", []).append(item["key"])
            continue
        content, how = cron_find_content(item["job_id"], item["run_dt"])
        if not content:
            log(f"CRON-LOSS {item['key']} content={how}")
            if not dry:
                st.setdefault("cron_handled", []).append(item["key"])
            continue
        half = "（企微群已有一份，这条只补微信腿）" if "wecom" in item["deliver"] else ""
        head = (f"⚠️【自动补发】{item['label']} 的微信腿被限流吞了"
                f"（{item['run_dt'].strftime('%m-%d %H:%M')} 投递失败）{half}，"
                f"本条由保障网经个人企微补上原文：\n\n")
        if dry:
            print(f"WOULD RESEND {item['key']} chars={len(content)} how={how} "
                  f"preview={content[:60]!r}")
            continue
        try:
            load_wecom_env()
            res = wecom_resend(head + content)
        except Exception as e:  # noqa: BLE001
            res = {"exception": repr(e)}
        ok = isinstance(res, dict) and res.get("success")
        log(f"CRON-LOSS {item['key']} chars={len(content)} how={how} "
            f"resend={'OK' if ok else res}")
        if ok:
            st.setdefault("cron_handled", []).append(item["key"])

    if not dry:
        st["last_ts"] = max_ts
        save_state(st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
