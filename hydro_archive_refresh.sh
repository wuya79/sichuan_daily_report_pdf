#!/bin/bash
# 四川水情二次归档（14:30）：取当日 00:00 快照 upsert 历史库
# 成功静默；失败输出一行由 cron 投递
cd /home/ubuntu/sichuan_hydro_price || exit 1
OUT=$(timeout 120 python3 hydro_archive_refresh.py 2>&1)
RC=$?
if [ $RC -ne 0 ]; then
  echo "❌ 水情二次归档失败/超时(RC=$RC): $(echo "$OUT" | tail -1)"
  exit 1
fi
[ -n "$OUT" ] && echo "$OUT"
exit 0
