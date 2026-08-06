#!/bin/bash
cd /home/ubuntu/v2_cq_strategy && python3 v2_daily.py 2>&1
echo ""
TARGET=$(date -d "+1 day" +%Y%m%d)
echo "📥 CSV下载: http://118.24.77.156:18080/reports/ml_strategy_${TARGET}.csv"
