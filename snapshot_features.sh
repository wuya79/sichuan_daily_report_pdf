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
  exit 0
else
  echo "snapshot FAIL: cp 源文件缺失? ($SRC)"
  exit 0
fi
