#!/bin/bash
# v2_daily.sh — 每日09:26 cron唯一生产入口
# v3.10: 推送摘要(双发微信+企微)+CSV下载链接; 技术日志不外发, 失败时推日志尾部
cd /home/ubuntu/v2_cq_strategy
python3 -m py_compile v2_daily.py 2>/dev/null
# v3.11.2(2026-08-20): 修管道吞错+失败降级策略
# 原 | tail -3 丢弃retrain退出码(静默); v3.11.1的exit1硬失败会让交易员失去策略(更糟)
# 正确: retrain失败→警告标注+照常跑策略(旧特征仍可推理); 仅v2_daily.py真失败才exit1
FEAT_WARN=""
timeout 600 python3 retrain.py --features-only > /tmp/v2_retrain_features.log 2>&1
if [ $? -ne 0 ]; then
  tail -3 /tmp/v2_retrain_features.log
  FEAT_WARN="⚠️ 特征重建失败,以下策略基于旧特征表(请留意)"
fi
OUTPUT=$(timeout 300 python3 v2_daily.py 2>&1)
RC=$?
if [ $RC -ne 0 ]; then
  echo "❌ 策略生成失败(exit $RC)，技术日志尾部:"
  echo "$OUTPUT" | tail -15
  exit 1
fi
echo "$OUTPUT" | sed -n '/===SUMMARY===/,/===END===/p' | grep -v '^==='
if [ -n "$FEAT_WARN" ]; then
  echo "$FEAT_WARN"
fi
