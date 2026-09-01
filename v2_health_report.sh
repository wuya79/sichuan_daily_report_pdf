#!/bin/bash
# v2_health_report.sh — 09:33 cron (2026-08-19: 曾试错峰09:35/09:40, 回退09:33; 水电摘要已移除切块引爆源)
# 直接出日报(2026-08-23: 影子V2分工已退役, 板块③渲染35d执行结算)
# tee落盘: 供10:05看门狗检测投递失败后原样补发(不重跑脚本, 防metrics归档重复)
# set -o pipefail: 防止tee吞掉v2_health_report.py的退出码(2026-08-19审计修复)
set -o pipefail
cd /home/ubuntu/v2_cq_strategy
D=$(date +%Y%m%d)
python3 v2_health_report.py | tee "output/v2_daily_report_${D}.txt"
