#!/usr/bin/env python3
"""
每日追加策略实际表现到回测CSV — 09:29 cron (等实际数据09:30发布)

逻辑：遍历归档目录，找到所有「运行日」的策略归档，
      对照运行日实际价差，计算PnL/准确率，追加到 hourly_decisions.csv
      已存在的日期跳过（幂等）。
"""
import json, pandas as pd, os, re, shutil
from datetime import date, timedelta

HDF = '/home/ubuntu/v2_cq_strategy/reports/hourly_decisions.csv'
ARCHIVE_DIR = '/home/ubuntu/v2_cq_strategy/output/archive'
PH = '/home/ubuntu/v2_cq_strategy/output/cq_price_history.json'
SPREAD_T = 5
ARCHIVE_RE = re.compile(r'^ml_strategy_(\d{4}-\d{2}-\d{2})\.json$')

# 1. 加载已有记录（用于去重）
existing_dates = set()
old_df = None
if os.path.exists(HDF):
    old_df = pd.read_csv(HDF)
    old_df['date'] = old_df['date'].astype(str)
    existing_dates = set(old_df['date'].unique())

# 2. 加载价格历史
with open(PH) as f:
    ph = json.load(f)
price_by_date = {d['date']: d for d in ph}

# 2.5. 自拉取缺失的价格数据（cq-web 真实值 09:30 后发布，v2_daily 09:21 拉不到）
#      在归档目录中找出有策略但缺价格的日期，补拉后写入 cq_price_history.json
import re as _re_v2, urllib.request as _ur_v2
_API_URL = 'http://127.0.0.1:45678/api/query'
_V_RE = _re_v2.compile(r'^V(\d{4})')

def _aggregate_to_hourly(raw):
    hh = {}
    for k, v in raw.items():
        minute = int(k[:2]) * 60 + int(k[2:])
        h = max(0, (minute - 1) // 60)
        hh.setdefault(h, []).append(v)
    return [sum(hh.get(h, [0])) / len(hh[h]) if h in hh else None for h in range(24)]

_missing_dates = set()
if os.path.exists(ARCHIVE_DIR):
    for _fn in os.listdir(ARCHIVE_DIR):
        _m = ARCHIVE_RE.match(_fn)
        if _m and _m.group(1) not in price_by_date:
            _missing_dates.add(_m.group(1))

if _missing_dates:
    print(f'价格缺失 {len(_missing_dates)} 天，尝试自拉取...')
    for _d in sorted(_missing_dates):
        try:
            _da_raw = {}; _rt_raw = {}
            for _tid, _store in [(5, _da_raw), (2, _rt_raw)]:
                _req = _ur_v2.Request(_API_URL,
                    json.dumps({"data_type": _tid, "search_date": _d}).encode(),
                    {"Content-Type": "application/json"})
                _r = json.loads(_ur_v2.urlopen(_req, timeout=15).read())
                for _row in _r.get("data", {}).get("rows", []):
                    if _row.get("数据类型") == "电能量价格":
                        for _k, _v in _row.items():
                            _m2 = _V_RE.match(_k)
                            if _m2 and _v not in (None, "", "-"):
                                _store[_m2.group(1)] = float(str(_v).replace(",", ""))
                        break
            _da_list = _aggregate_to_hourly(_da_raw)
            _rt_list = _aggregate_to_hourly(_rt_raw)
            if any(v is not None for v in _da_list) and any(v is not None for v in _rt_list):
                ph.append({"date": _d, "da": _da_list, "rt": _rt_list})
                price_by_date[_d] = ph[-1]
                _tmp = PH + '.tmp'
                with open(_tmp, 'w') as _fh:
                    json.dump(ph, _fh)
                os.replace(_tmp, PH)
                print(f'  ✅ {_d}')
            else:
                print(f'  ⚠ {_d}: 数据无效')
        except Exception as _e:
            print(f'  ⚠ {_d}: 拉取失败({_e})')

# 3. 遍历归档，追加未处理的运行日
if not os.path.exists(ARCHIVE_DIR):
    print(f'归档目录不存在: {ARCHIVE_DIR}')
    exit(0)

appended = 0
for fname in sorted(os.listdir(ARCHIVE_DIR)):
    m = ARCHIVE_RE.match(fname)
    if not m: continue
    run_date = m.group(1)
    
    # 已追加 → 跳过
    if run_date in existing_dates:
        continue
    
    # 实际价格尚未发布 → 跳过（等下次 cron）
    prices = price_by_date.get(run_date)
    if not prices:
        print(f'  {run_date}: 价格数据未发布，跳过')
        continue
    
    # 加载策略
    archive_path = os.path.join(ARCHIVE_DIR, fname)
    try:
        v2 = json.load(open(archive_path))
    except Exception as e:
        print(f'  {run_date}: 读取失败({e})，跳过')
        continue
    
    # 匹配计算
    # regime标签(顶层, 预测日当时的市场状态; 2026-08-19审计修复: 原硬编码空字符串)
    _rg = v2.get('regime') or {}
    _regime_label = _rg.get('label', '') if isinstance(_rg, dict) else str(_rg)
    rows = []
    for h in v2.get('hours', []):
        hh = int(h['hour'][:2])
        da = prices['da'][hh] if hh < len(prices.get('da', [])) else None
        rt = prices['rt'][hh] if hh < len(prices.get('rt', [])) else None
        if da is None or rt is None:
            continue
        spread = rt - da
        action = h.get('action', '—')
        
        correct = None
        if action == '做多' and spread > SPREAD_T:
            correct = True
        elif action == '做少' and spread < -SPREAD_T:
            correct = True
        elif action in ('做多', '做少') and abs(spread) > SPREAD_T:
            correct = False
        
        pnl = 0
        if action == '做多' and abs(spread) > SPREAD_T:
            pnl = spread
        elif action == '做少' and abs(spread) > SPREAD_T:
            pnl = -spread
        
        rows.append({
            'date': run_date, 'hour': hh, 'action': action,
            'spread': spread, 'pnl_equal': pnl, 'correct': correct,
            'prob_long': h.get('prob_long'), 'prob_short': h.get('prob_short'),
            'pred_spread': h.get('pred_spread'),
            'position_multiplier': h.get('position_multiplier', 1.0),
            'pnl_pos_engine': pnl * h.get('position_multiplier', 1.0) if abs(spread) > SPREAD_T else 0,
            'regime': _regime_label, 'segment': h.get('hour_type', ''),
        })
    
    if not rows:
        print(f'  {run_date}: 无有效时段，跳过')
        continue
    
    new_df = pd.DataFrame(rows)
    if old_df is not None:
        combined = pd.concat([old_df, new_df], ignore_index=True)
    else:
        combined = new_df
    # 原子写(临时文件+replace), 防日报并发读半截; 写前备份防写坏不可恢复
    if os.path.exists(HDF):
        shutil.copy2(HDF, HDF + '.bak')
    _h_tmp = HDF + '.tmp'
    combined.to_csv(_h_tmp, index=False)
    os.replace(_h_tmp, HDF)
    
    # 更新内存状态（后续日期去重）
    old_df = combined
    existing_dates.add(run_date)
    appended += 1
    print(f'  ✅ {run_date}: {len(rows)}笔')

print(f'本次追加 {appended} 天')
