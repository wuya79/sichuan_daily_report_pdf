#!/bin/bash
# 微信投递保障网 — cron 入口（venv python；stdout 保持静默）
/home/ubuntu/.hermes/hermes-agent/venv/bin/python3 /home/ubuntu/.hermes/scripts/wx_delivery_guard.py "$@"
