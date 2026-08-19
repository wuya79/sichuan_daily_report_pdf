#!/bin/bash
# v2_retrain.sh — 每日08:10 cron (2026-08-18改为每天, 原周六)
# 全量重训XGBoost模型 (cls×3 + reg), 校准, 备份
# 互斥: 与25d重训共用锁, 防API故障日双进程并发写特征表
# timeout 1800: 单日最坏~600s(API全重试), 3天累积~1800s, 留足余量防"永远追不上"
exec 9>/tmp/v2_retrain.lock
flock -n 9 || { echo "另一个retrain正在运行, 跳过本次"; exit 0; }
cd /home/ubuntu/v2_cq_strategy && timeout 1800 python3 retrain.py 2>&1
