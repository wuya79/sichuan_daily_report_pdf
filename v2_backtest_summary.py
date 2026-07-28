#!/usr/bin/env python3
"""V2回测日报 — 展示最近一日的回测结果"""
import pandas as pd
from datetime import date

HDF = '/home/ubuntu/v2_cq_strategy/reports/hourly_decisions.csv'
hdf = pd.read_csv(HDF)
hdf['date'] = hdf['date'].astype(str)
act = hdf[(hdf['action'] != '观望') & (hdf['spread'].abs() > 5)]

# 全量汇总
eq = act['pnl_equal'].sum()
pe = act['pnl_pos_engine'].sum() if 'pnl_pos_engine' in act.columns else 0
acc = act['correct'].mean() * 100
lc = (act['action'] == '做多').sum()
sc = (act['action'] == '做少').sum()
avg_m = act['position_multiplier'].mean() if 'position_multiplier' in act.columns else 0

# 最近一天
dates = sorted(hdf['date'].unique())
yesterday = (date.today() - __import__('datetime').timedelta(days=1)).isoformat()
last_date = yesterday if yesterday in dates else dates[-1]
d = act[act['date'] == last_date]
d_eq = d['pnl_equal'].sum()
d_acc = d['correct'].mean() * 100
d_lc = (d['action'] == '做多').sum()
d_sc = (d['action'] == '做少').sum()

print('V2回测 %s' % date.today())
print('全量: M1=%+.0f  仓位=%+.0f(+%.0f%%)  Acc=%.1f%%  做多=%d 做空=%d  mul=%.2f' % (eq, pe, (pe/eq-1)*100 if eq else 0, acc, lc, sc, avg_m))
print('最新(%s): PnL=%+.0f  Acc=%.1f%%  long=%d short=%d' % (last_date, d_eq, d_acc, d_lc, d_sc))
