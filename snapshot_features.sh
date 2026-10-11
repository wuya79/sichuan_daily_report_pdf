#!/bin/bash
# CQ特征表每日快照 — 2026-10-11建 (生产等价回测基建: 保证"未来回测能复现当时")
# 失败输出非空(告警), 成功静默 (与no_agent watchdog模式兼容)
SRC=/home/ubuntu/v2_cq_strategy/data/features_enhanced.csv
DST=/home/ubuntu/v2_cq_strategy/data/snapshots
mkdir -p "$DST" || { echo "snapshot FAIL: mkdir $DST"; exit 0; }
D=$(date +%Y%m%d)
if [ -f "$DST/features_$D.csv.gz" ]; then exit 0; fi
if cp "$SRC" "$DST/features_$D.csv" 2>/dev/null; then
  gzip -f "$DST/features_$D.csv" || { echo "snapshot FAIL: gzip"; exit 0; }
  find "$DST" -name "features_*.csv.gz" -mtime +730 -delete 2>/dev/null
else
  echo "snapshot FAIL: cp 源文件缺失? ($SRC)"
  exit 0
fi
# 新鲜度校验(2026-10-11增): 表尾应含昨日行——不含=晨链未更新(快照已存, 语义上=当时实际状态, 但告警提示)
Y=$(date -d "yesterday" +%Y-%m-%d)
if [ -n "$Y" ] && ! zcat "$DST/features_$D.csv.gz" 2>/dev/null | grep -q "^$Y,"; then
  echo "snapshot WARN: 表中未见昨日($Y)行 — 晨链未更新? 快照已存但请检查"
fi
exit 0
