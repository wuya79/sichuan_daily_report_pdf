#!/bin/bash
# 企微单聊主动消息探测 — 结果写日志；stdout 保持静默（no_agent cron 静默运行）
export $(grep -E '^WECOM_' /home/ubuntu/.hermes/.env | xargs)
/home/ubuntu/.hermes/hermes-agent/venv/bin/python3 - <<'PYEOF'
import asyncio, sys, os, datetime
sys.path.insert(0, '/home/ubuntu/.hermes/hermes-agent')
LOG = os.path.expanduser('~/.hermes/logs/wecom_probe_0915.log')

async def run():
    from gateway.config import PlatformConfig
    from plugins.platforms.wecom.adapter import _standalone_send
    pconfig = PlatformConfig(
        enabled=True,
        extra={"bot_id": os.environ.get("WECOM_BOT_ID", ""), "secret": os.environ.get("WECOM_SECRET", "")},
    )
    return await _standalone_send(
        pconfig, "QiuLing",
        "🔧【通道测试】Hermes 个人企微通道探测——看到这条说明『推送到你个人企业微信』已打通。",
    )

try:
    res = asyncio.run(run())
except Exception as e:
    res = {'exception': repr(e)}

with open(LOG, 'a', encoding='utf-8') as f:
    f.write(f"{datetime.datetime.now().isoformat()} | probe | {res}\n")
PYEOF
exit 0
