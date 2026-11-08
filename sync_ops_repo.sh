#!/usr/bin/env bash
# 每日同步 skills + 运行数据到私有备份仓 wuya79/sichuan_trading_ops
# no_agent cron 用：无变更=静默；成功=一行；失败=一行告警
set -uo pipefail
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=no"
REPO=/home/ubuntu/sichuan_trading_ops
LOG=/tmp/ops_sync_last.log
cd "$REPO" || { echo "⚠️ ops 同步失败：仓库目录不存在"; exit 0; }
BEFORE=$(git rev-parse --short HEAD 2>/dev/null || echo none)
if ! ./sync_ops.sh >"$LOG" 2>&1; then
  echo "⚠️ ops 同步失败 $(date '+%m-%d %H:%M')：$(tail -2 "$LOG" | tr '\n' ' ')"
  exit 0
fi
AFTER=$(git rev-parse --short HEAD 2>/dev/null || echo none)
[ "$BEFORE" = "$AFTER" ] && exit 0
echo "ops 同步 ✅ $(date '+%m-%d %H:%M') commit $AFTER ｜ skills $(git ls-files skills | wc -l) 文件 ｜ runtime $(git ls-files runtime | wc -l) 文件"
