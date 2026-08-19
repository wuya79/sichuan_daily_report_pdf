#!/bin/bash
# v2_daily.sh — 每日09:26 cron唯一生产入口
# v3.10: 推送摘要(双发微信+企微)+CSV下载链接; 技术日志不外发, 失败时推日志尾部
cd /home/ubuntu/v2_cq_strategy
python3 -m py_compile v2_daily.py 2>/dev/null
timeout 600 python3 retrain.py --features-only 2>&1 | tail -3
OUTPUT=$(timeout 300 python3 v2_daily.py 2>&1)
RC=$?
if [ $RC -ne 0 ]; then
  echo "❌ 策略生成失败(exit $RC)，技术日志尾部:"
  echo "$OUTPUT" | tail -15
  exit 1
fi
echo "$OUTPUT" | sed -n '/===SUMMARY===/,/===END===/p' | grep -v '^==='
