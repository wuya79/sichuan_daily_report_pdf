#!/usr/bin/env python3
"""早晨报告看门狗 — 10:05 cron (no_agent)

检查水电日报(55e76a6c0f42)与V2策略日报(c9575c5b6af8)今日投递状态：
- 主推送投递失败(last_delivery_error 非空 且 last_run_at=今天)
  → 直发【个人企微单聊】(不走微信、不进群)；已由保障网补过则跳过
- 个人企微发送失败 → 把正文打到 stdout，让 cron 的微信腿再兜一次
- 全部正常 → 输出空(静默)
- 永不 exit 非0，避免看门狗自身触发 cron 错误告警

2026-11-16 改：补发改走个人企微（微信群聊打扰），与 wx_delivery_guard 共享
.cron_handled 去重键 `cron-<job_id>-<YYYYmmdd_HHMMSS>`。
"""
import datetime
import json
import os
import sys

JOBS_JSON = "/home/ubuntu/.hermes/cron/jobs.json"
GUARD_STATE = "/home/ubuntu/.hermes/scripts/.wx_guard_state.json"
TODAY = datetime.date.today().isoformat()
WECOM_DM = "QiuLing"
HOME = os.path.expanduser("~")

# job_id -> (报告名, 内容文件路径)
JOBS = {
    "55e76a6c0f42": ("四川水电日报", "/home/ubuntu/sichuan_hydro_price/latest_summary.txt"),
    "c9575c5b6af8": ("V2策略日报",
                     "/home/ubuntu/v2_cq_strategy/output/v2_daily_report_{}.txt".format(TODAY.replace("-", ""))),
}


def job_key(jid: str, run_iso: str) -> str:
    try:
        t = datetime.datetime.fromisoformat(run_iso)
        return f"cron-{jid}-{t.strftime('%Y%m%d_%H%M%S')}"
    except Exception:
        return f"cron-{jid}-{run_iso[:19]}"


def load_guard_state() -> dict:
    try:
        with open(GUARD_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_ts": 0.0, "handled": [], "pending": [], "cron_handled": []}


def save_guard_state(st: dict) -> None:
    """只合并本脚本关心的 cron_handled，保留其余字段不动。"""
    st["cron_handled"] = list(dict.fromkeys(st.get("cron_handled", [])))[-500:]
    tmp = GUARD_STATE + ".wd.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, GUARD_STATE)


def load_wecom_env() -> None:
    try:
        for line in open(f"{HOME}/.hermes/.env", encoding="utf-8"):
            line = line.strip()
            if line.startswith("WECOM_") and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)
    except Exception:
        pass


def send_dm(text: str) -> bool:
    import asyncio

    load_wecom_env()
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

    try:
        res = asyncio.run(run())
    except Exception:
        return False
    return isinstance(res, dict) and bool(res.get("success"))


def main() -> None:
    try:
        with open(JOBS_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    jobs = data.get("jobs", []) if isinstance(data, dict) else []

    gst = load_guard_state()
    done_keys = set(gst.get("cron_handled") or [])
    sent_keys, weixin_fallback = [], []

    for jid, (label, content_path) in JOBS.items():
        job = next((j for j in jobs if j.get("id") == jid), None)
        if job is None:
            continue
        last_run = job.get("last_run_at") or ""
        if last_run[:10] != TODAY:
            continue
        if not job.get("last_delivery_error"):
            continue
        key = job_key(jid, last_run)
        if key in done_keys:
            continue  # 保障网已补过，别重复打扰
        if not os.path.exists(content_path):
            weixin_fallback.append(f"⚠️ {label}今日推送失败且内容文件缺失({content_path})，请人工检查")
            continue
        with open(content_path, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            weixin_fallback.append(f"⚠️ {label}今日推送失败且内容为空({content_path})，请人工检查")
            continue
        text = (f"🔁【看门狗·个人企微补发】{label} 今日微信推送被限流吞了，"
                f"原文如下：\n\n{content}")
        if send_dm(text):
            sent_keys.append(key)
        else:
            # 个人企微也失败 → 交回 cron 的微信腿再试一次
            weixin_fallback.append(f"🔁 {label}补发（主推送被限流）\n\n{content}")

    if sent_keys:
        gst["cron_handled"] = list(done_keys | set(sent_keys))
        try:
            save_guard_state(gst)
        except Exception:
            pass

    if weixin_fallback:
        print("\n\n".join(weixin_fallback))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # 看门狗永不因自身异常触发 cron 错误告警
