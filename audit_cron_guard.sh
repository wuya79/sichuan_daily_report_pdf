#!/bin/bash
# cron治理审计守护 — 每日16:00 (2026-08-20 v2: 成功也推送一行确认)
# 成功: 一行✅(推送); 失败: 失败项摘要(推送)+exit1
OUT=$(python3 /home/ubuntu/.hermes/scripts/audit_cron_changes.py 2>&1)
RC=$?
if [ $RC -ne 0 ]; then
  echo "⚠️ cron治理审计发现异常:"
  echo "$OUT" | grep -E '^(BAD|DIFF|MISS|FAIL|ALERT|HIT)'
  echo "--- 失败项清单 ---"
  echo "$OUT" | grep -E '^  §'
  echo "---"
  echo "完整报告: python3 ~/.hermes/scripts/audit_cron_changes.py"
  exit 1
fi
echo "✅ cron治理审计通过 | $(date +%m-%d) | 11项全OK(引用/镜像/权限/gitignore/语法/失败模式/敏感/关键文件/cron/文档漂移/代码dirty)"
exit 0
