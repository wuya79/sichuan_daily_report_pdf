#!/usr/bin/env python3
"""早晨报告看门狗 — 10:05 cron (no_agent)
检查水电日报(55e76a6c0f42)与V2策略日报(c9575c5b6af8)今日投递状态:
- 主推送投递失败(last_delivery_error非空 且 last_run_at=今天) → 重发对应内容
- 全部正常 → 输出空(静默, no_agent空stdout不打扰)
- 永不exit非0, 避免看门狗自身触发cron错误告警
"""
import datetime
import json
import os

JOBS_JSON = "/home/ubuntu/.hermes/cron/jobs.json"
TODAY = datetime.date.today().isoformat()

# job_id -> (报告名, 内容文件路径, 内容文件说明)
JOBS = {
    "55e76a6c0f42": ("四川水电日报", "/home/ubuntu/sichuan_hydro_price/latest_summary.txt"),
    "c9575c5b6af8": ("V2策略日报", "/home/ubuntu/v2_cq_strategy/output/v2_daily_report_{}.txt".format(TODAY.replace("-", ""))),
}


def main() -> None:
    try:
        with open(JOBS_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    jobs = data.get("jobs", []) if isinstance(data, dict) else []

    resends = []
    for jid, (label, content_path) in JOBS.items():
        job = next((j for j in jobs if j.get("id") == jid), None)
        if job is None:
            continue
        last_run = job.get("last_run_at") or ""
        run_today = last_run[:10] == TODAY
        delivery_err = job.get("last_delivery_error")
        if run_today and delivery_err:
            if os.path.exists(content_path):
                with open(content_path, encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    resends.append(f"🔁 {label}补发（主推送被限流）\n\n{content}")
            else:
                resends.append(f"⚠️ {label}今日推送失败且内容文件缺失({content_path})，请人工检查")

    if resends:
        print("\n\n".join(resends))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # 看门狗永不因自身异常触发cron错误告警

