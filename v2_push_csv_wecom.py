#!/usr/bin/env python3
"""V2策略CSV原生附件推送企微群 — 成功静默(exit 0), 失败 exit 1 + stderr
调用方: v2_single_stage.py 第5.5步 (hermes venv python)
CSV定位: /var/www/reports/ml_strategy_*.csv 中 mtime 最新且为今日生成
        (用 mtime 而非自算 D+1 日期, 规避周末/节假日命名错配)
"""
import asyncio
import glob
import os
import sys
from datetime import date, datetime

CHAT_ID = 'wrLOxVPQAAIZErQr35Q3XtROvqCP_DUQ'
PUBLIC_DIR = '/var/www/reports'


def find_today_csv():
    files = glob.glob(os.path.join(PUBLIC_DIR, 'ml_strategy_*.csv'))
    if not files:
        return None
    latest = max(files, key=os.path.getmtime)
    if datetime.fromtimestamp(os.path.getmtime(latest)).date() < date.today():
        return None  # 最新的也不是今天生成的 → 今日未产出
    return latest


async def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else find_today_csv()
    if not csv_path or not os.path.exists(csv_path):
        print('v2_push_csv_wecom: 今日无ml_strategy CSV', file=sys.stderr)
        return 1

    sys.path.insert(0, '/home/ubuntu/.hermes/hermes-agent')
    from gateway.config import PlatformConfig
    from plugins.platforms.wecom.adapter import WeComAdapter

    adapter = WeComAdapter(PlatformConfig(
        enabled=True,
        extra={
            'bot_id': os.getenv('WECOM_BOT_ID', ''),
            'secret': os.getenv('WECOM_SECRET', ''),
        }))
    if not await adapter.connect():
        print('v2_push_csv_wecom: connect失败 '
              f'{getattr(adapter, "fatal_error_message", "unknown")}',
              file=sys.stderr)
        return 1
    try:
        result = await adapter.send_document(CHAT_ID, csv_path)
        if not result.success:
            print(f'v2_push_csv_wecom: 发送失败 {result.error}', file=sys.stderr)
            return 1
    finally:
        await adapter.disconnect()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
