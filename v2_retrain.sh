#!/bin/bash
# v2_retrain.sh — 周六08:00 cron
# retrain.py 已内联方向特征计算，不再需要 add_features.py
cd /home/ubuntu/v2_cq_strategy && python3 retrain.py 2>&1
