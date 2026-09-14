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
    return {"last_ts": 0.0, "handled": [], "pending": []}


def save_state(st: dict) -> None:
    st["handled"] = st["handled"][-500:]
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
    st = load_state()
    if dry:
        if "--since" in sys.argv:
            since = parse_ts(sys.argv[sys.argv.index("--since") + 1] + ",000")
        else:
            since = time.time() - 86400
    else:
        since = st.get("last_ts", 0.0) or (time.time() - 300)

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

    if not dry:
        st["last_ts"] = max_ts
        save_state(st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
