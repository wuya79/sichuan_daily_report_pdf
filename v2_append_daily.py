#!/usr/bin/env python3
"""每日追加昨天V2决策结果到回测CSV。在v2_daily之后运行。"""
import json, pandas as pd, os
from datetime import date, timedelta

HDF = '/home/ubuntu/v2_cq_strategy/reports/hourly_decisions.csv'
V2_OUT = '/home/ubuntu/v2_cq_strategy/output/ml_strategy_latest.json'
PH = '/home/ubuntu/v2_cq_strategy/output/cq_price_history.json'
SPREAD_T = 5

yesterday = (date.today() - timedelta(days=1)).isoformat()

# 1. 读取V2昨天预测
v2 = json.load(open(V2_OUT))
if v2['date'] != yesterday:
    print('V2预测日期=%s 不是昨天=%s，跳过' % (v2['date'], yesterday))
    exit(0)

# 2. 读取昨天实际价格
ph = json.load(open(PH))
prices = next((d for d in ph if d['date'] == yesterday), None)
if not prices:
    print('价格历史无%s' % yesterday)
    exit(1)

# 3. 匹配计算
rows = []
for h in v2['hours']:
    hh = int(h['hour'][:2])
    da = prices['da'][hh] if hh < len(prices.get('da',[])) else None
    rt = prices['rt'][hh] if hh < len(prices.get('rt',[])) else None
    if da is None or rt is None: continue
    spread = rt - da
    action = h.get('action','—')
    if action not in ('做多','做少'): action = h.get('action','—')
    
    correct = None
    if action == '做多' and spread > SPREAD_T: correct = True
    elif action == '做少' and spread < -SPREAD_T: correct = True
    elif action in ('做多','做少') and abs(spread) > SPREAD_T: correct = False
    
    pnl = 0
    if action == '做多' and abs(spread) > SPREAD_T: pnl = spread
    elif action == '做少' and abs(spread) > SPREAD_T: pnl = -spread
    
    rows.append({
        'date': yesterday, 'hour': hh, 'action': action,
        'spread': spread, 'pnl_equal': pnl, 'correct': correct,
        'prob_long': h.get('prob_long'), 'prob_short': h.get('prob_short'),
        'pred_spread': h.get('pred_spread'),
        'position_multiplier': h.get('position_multiplier', 1.0),
        'pnl_pos_engine': pnl * h.get('position_multiplier', 1.0) if abs(spread)>SPREAD_T else 0,
        'regime': '', 'segment': h.get('hour_type',''),
    })

# 4. 追加到CSV
new_df = pd.DataFrame(rows)
if os.path.exists(HDF):
    old = pd.read_csv(HDF)
    old['date'] = old['date'].astype(str)
    if yesterday in old['date'].values:
        print('%s 已存在，跳过' % yesterday)
        exit(0)
    combined = pd.concat([old, new_df], ignore_index=True)
else:
    combined = new_df

combined.to_csv(HDF, index=False)
print('已追加 %s: %d笔' % (yesterday, len(rows)))
