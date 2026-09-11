#!/bin/bash
# cron治理审计守护 — 每日16:00 (2026-08-20 v2: 成功也推送一行确认)
# 成功: 一行✅(推送); 失败: 失败项摘要(推送)+exit1
# 2026-09-11 v3: 增"备份链"第12项(读当日备份任务cron输出; 09-06~10静默失败事故的可见化; 测试可覆写 BK_DIR)
OUT=$(python3 /home/ubuntu/.hermes/scripts/audit_cron_changes.py 2>&1)
RC=$?

# ── 第12项: 备份链(当日10:15备份任务是否成功推送) ──
BK_DIR="${BK_DIR:-/home/ubuntu/.hermes/cron/output/0ab7988f9358}"
D=$(date +%Y-%m-%d)
BK_F=""
for _f in "$BK_DIR/${D}"_*.md; do [ -e "$_f" ] && BK_F="$_f"; done   # 勿用|tail(§6吞错管道扫描命中)
BK_RC=0
if [ -z "$BK_F" ]; then
  if [ "$(date +%s)" -lt "$(date -d "$D 10:15" +%s)" ]; then BK_MSG="备份链⏳(未到10:15窗口)"
  else BK_RC=1; BK_MSG="备份链⚠️: 当日($D)无备份任务输出(任务未跑/异常)"; fi
elif grep -q "备份失败" "$BK_F"; then
  BK_RC=1; BK_MSG="备份链❌: $(grep -m1 '备份失败' "$BK_F" | cut -c1-110)"
elif grep -qE "备份已推送|远端已是最新" "$BK_F"; then
  BK_MSG="备份链✅($(grep -m1 -E '备份已推送|远端已是最新' "$BK_F" | sed 's/^✅ *//' | cut -c1-90))"
else
  BK_RC=1; BK_MSG="备份链⚠️: 输出无法判定(无成功/失败关键字)"
fi

if [ $RC -ne 0 ] || [ $BK_RC -ne 0 ]; then
  echo "⚠️ cron治理审计发现异常:"
  if [ $RC -ne 0 ]; then
    echo "$OUT" | grep -E '^(BAD|DIFF|MISS|FAIL|ALERT|HIT)'
    echo "--- 失败项清单 ---"
    echo "$OUT" | grep -E '^  §'
  fi
  [ $BK_RC -ne 0 ] && echo "$BK_MSG"
  echo "---"
  echo "完整报告: python3 ~/.hermes/scripts/audit_cron_changes.py"
  exit 1
fi
echo "✅ cron治理审计通过 | $(date +%m-%d) | 12项全OK(引用/镜像/权限/gitignore/语法/失败模式/敏感/关键文件/cron/文档漂移/代码dirty/备份链) | $BK_MSG"
exit 0
