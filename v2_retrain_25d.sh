#!/bin/bash
# v2_retrain_25d.sh — 每日08:00 cron
# 短窗口(35天, P2)重训XGBoost, 保存到 models/*_25d.json
# 互斥: 与全量重训共用锁, 防并发写特征表
# timeout 1800: 单日最坏~600s(API全重试), 3天累积~1800s, 留足余量防"永远追不上"
exec 9>/tmp/v2_retrain.lock
flock -n 9 || { echo "另一个retrain正在运行, 跳过本次"; exit 0; }
cd /home/ubuntu/v2_cq_strategy && timeout 1800 python3 retrain.py --train-days 35 2>&1
