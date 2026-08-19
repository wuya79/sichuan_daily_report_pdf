#!/bin/bash
# 四川日报 no_agent 直推：摘要+全文链接（2026-08-19改）
# 原版全文5994字符被微信切成3块连发, 触发iLink限流(2026-08-19实测);
# 改为摘要(头部概览+⑨策略段, ~800字符单条) + 全文txt链接
cd /home/ubuntu/sichuan_hydro_price
OUTPUT=$(timeout 240 python3 daily_report.py 2>/dev/null)
RC=$?
if [ $RC -ne 0 ]; then
  echo "❌ 日报生成失败或超时，请手动检查"
  exit 1
fi
FULL_TMP="/tmp/sichuan_report_full_$(date +%Y%m%d).txt"
echo "$OUTPUT" | grep -v "^✅" > "$FULL_TMP"
python3 scripts/sichuan_report_summary.py "$FULL_TMP"
