#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""镜像切主力·首晨链自动晨检（2026-09-15 起 5 天，每天 10:08；只读）
①镜像归档统计 ②重庆融合耗时 ③产物新鲜度 ④交易口健康探测
输出→cron 直投（origin）。无异常也输出摘要（确认检查确实跑了）。"""
import json
import os
import time
import urllib.request
from collections import Counter

TODAY = time.strftime('%Y-%m-%d')
out = []


def p(s):
    out.append(s)


p('🔍 镜像晨检 · %s' % TODAY)

# ① 镜像归档
arch = os.path.expanduser('~/.hermes/archive_sc/raw_daily/%s.jsonl' % TODAY)
if os.path.exists(arch):
    recs = []
    for line in open(arch):
        try:
            recs.append(json.loads(line))
        except Exception:
            pass
    src = Counter(str(r.get('source')) for r in recs)
    win = [r for r in recs if r.get('source') == 'new(win)']
    bad = [r for r in recs if r.get('level') in ('struct', 'mismatch')
           or str(r.get('new', '')).startswith('err') or str(r.get('old', '')).startswith('err')]
    p('① 镜像归档: %d 条 | 来源 %s' % (len(recs), dict(src)))
    p('   救回(new-win): %d %s' % (len(win), '、'.join('t%s' % r.get('type') for r in win[:10])))
    p('   分歧/结构/报错: %d' % len(bad))
    for r in bad[:8]:
        p('   ⚠ t%s@%s src=%s new=%s old=%s lvl=%s' % (r.get('type'), r.get('date'),
          r.get('source'), r.get('new'), r.get('old'), r.get('level')))
else:
    p('① 镜像归档: ❌ 文件不存在(双跑未发生?)')

# ② 重庆融合
flog = '/home/ubuntu/v2_cq_strategy/output/fusion_prefetch.log'
try:
    all_lines = open(flog, encoding='utf-8').read().splitlines()
    todays = [l for l in all_lines if l.startswith('[%s' % TODAY)]
    runs = [l for l in todays if 'run:' in l]
    morning = [l for l in todays if len(l) > 17 and l[12:17] >= '09:00']
    trade_n = sum(1 for l in morning if 'trade' in l)
    last = runs[-1].split('] ', 1)[-1] if runs else '无运行记录'
    p('② 重庆融合: 今日run=%d | 末次: %s' % (len(runs), last[:130]))
    p('   上午(≥09:00) trade相关行: %d (期望0)' % trade_n)
except Exception as e:
    p('② 重庆融合: 日志异常 %s' % e)


# ③ 产物新鲜度
def fresh(path, hhmm):
    try:
        ts = time.strftime('%m-%d %H:%M', time.localtime(os.path.getmtime(path)))
        ok = ts.startswith(TODAY[5:]) and ts[-5:] >= hhmm
        return '%s%s' % (ts, '✓' if ok else '✗')
    except Exception:
        return '缺失✗'


p('③ 产物: 日报%s | 发电侧%s | V2日报%s' % (
    fresh('/home/ubuntu/sichuan_hydro_price/latest_report.txt', '09:25'),
    fresh(os.path.expanduser('~/.hermes/logs/gen_side_txt.log'), '09:45'),
    fresh('/home/ubuntu/v2_cq_strategy/output/v2_daily_report_%s.txt' % TODAY.replace('-', ''), '09:30')))

# ④ 交易口探测（为"恢复后改回"提供依据；3s 超时）
try:
    req = urllib.request.Request(
        'http://127.0.0.1:45678/api/trade',
        data=json.dumps({'data_type': 2, 'info_date': TODAY}).encode(),
        headers={'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            p('④ 交易口: HTTP %s (或已恢复, %.1fs)' % (r.status, time.time() - t0))
    except Exception as e:
        p('④ 交易口: 仍挂(%s, %.1fs)' % (type(e).__name__, time.time() - t0))
except Exception as e:
    p('④ 交易口: 探测异常 %s' % e)

print('\n'.join(out))
