#!/usr/bin/env python3
"""
每日追加策略实际表现到回测CSV — 09:29 cron (等实际数据09:30发布)

逻辑：遍历归档目录，找到所有「运行日」的策略归档，
      对照运行日实际价差，计算PnL/准确率，追加到 hourly_decisions.csv
      已存在的日期跳过（幂等）。

2026-09-11 改造(用户批准):
- stdout 只在异常时输出(异常才推送用户); 过程明细写 output/append_daily.log
- 自拉取与 v2_fetch_price 同口径: 系统t2空→交易t12兜底; 完整性校验(24h无None且不全0)
- 异常判定: 复盘结算滞后(最新<D-2) / 价格缺失超窗 / 有价未入账

命名说明(2026-09-11, 审计P3): 读写的 `action_25d`/`position_multiplier_25d` 等列名中, `_25d` 为
35d 系历史命名(模型文件实为 *_35d.json), 属策略归档/35d_settle.csv 的跨系统契约, 禁止单方面改名。
"""
import json, pandas as pd, os, re, shutil, sys
from datetime import date, timedelta

# ── 输出重定向: 过程→日志文件, stdout仅异常 (2026-09-11) ──
_LOGF = open('/home/ubuntu/v2_cq_strategy/output/append_daily.log', 'a')
_STDOUT = sys.stdout
def print(*args, **kw):  # noqa: A001
    try:
        _LOGF.write(' '.join(str(a) for a in args) + '\n'); _LOGF.flush()
    except Exception:
        pass
_PROBLEMS = []
def problem(msg):
    _PROBLEMS.append(str(msg))

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
            if not _rt_raw:
                # 2026-09-11: 与v2_fetch_price同口径 — 系统t2空→交易t12兜底
                try:
                    _treq = _ur_v2.Request('http://127.0.0.1:45678/api/trade',
                        json.dumps({"data_type": 12, "info_date": _d}).encode(),
                        {"Content-Type": "application/json"})
                    _tr = json.loads(_ur_v2.urlopen(_treq, timeout=15).read())
                    for _x in _tr.get("data", []) or []:
                        try:
                            if str(_x.get("info_type")) != "1": continue
                            _hh, _mm = str(_x.get("info_time")).split(':')
                            _rt_raw[f"{int(_hh):02d}{int(_mm):02d}"] = float(str(_x.get("info_value")).replace(",", ""))
                        except Exception:
                            pass
                    if _rt_raw:
                        print(f'  ℹ {_d}: rt系统空 → 交易t12兜底 {len(_rt_raw)}点')
                except Exception as _te:
                    print(f'  ⚠ {_d}: 交易t12兜底失败({_te})')
            _da_list = _aggregate_to_hourly(_da_raw)
            _rt_list = _aggregate_to_hourly(_rt_raw)
            _da_ok = len(_da_list) == 24 and all(v is not None for v in _da_list) and not all(v == 0 for v in _da_list)
            _rt_ok = len(_rt_list) == 24 and all(v is not None for v in _rt_list) and not all(v == 0 for v in _rt_list)
            if _da_ok and _rt_ok:
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


# ============================================================
# 4. 35d执行结算 (2026-08-23新增): 从归档读action_25d/position_multiplier_25d
#    独立幂等遍历(35d_settle.csv自己的existing集合, 不受全量existing影响→首次跑自动回填历史)
#    ⚠️ 口径: 等权±spread + 仓位口径×乘数; 08-23起乘数含P_big加权(口径断点, 日报披露)
#    ⚠️ 失败不阻塞全量结算主流程
# ============================================================
S35 = '/home/ubuntu/v2_cq_strategy/output/35d_settle.csv'
try:
    _s35_existing = set()
    _s35_old = None
    if os.path.exists(S35):
        _s35_old = pd.read_csv(S35)
        _s35_old['date'] = _s35_old['date'].astype(str)
        _s35_existing = set(_s35_old['date'].unique())
    _s35_rows = []
    for fname in sorted(os.listdir(ARCHIVE_DIR)):
        m = ARCHIVE_RE.match(fname)
        if not m:
            continue
        run_date = m.group(1)
        if run_date in _s35_existing:
            continue
        prices = price_by_date.get(run_date)
        if not prices:
            continue
        try:
            v2 = json.load(open(os.path.join(ARCHIVE_DIR, fname)))
        except Exception:
            continue
        _day_rows = []
        for h in v2.get('hours', []):
            hh = int(h['hour'][:2])
            da = prices['da'][hh] if hh < len(prices.get('da', [])) else None
            rt = prices['rt'][hh] if hh < len(prices.get('rt', [])) else None
            if da is None or rt is None:
                continue
            spread = rt - da
            a25 = h.get('action_25d')
            if a25 not in ('做多', '做少') or abs(spread) <= SPREAD_T:
                continue
            pnl_eq = spread if a25 == '做多' else -spread
            _m25 = h.get('position_multiplier_25d')
            _m25 = float(_m25) if _m25 is not None else 1.0
            _day_rows.append({
                'date': run_date, 'hour': hh, 'action_25d': a25,
                'spread': spread, 'pnl_equal_35d': pnl_eq,
                'pnl_pos_35d': pnl_eq * _m25,
            })
        if _day_rows:
            _s35_rows.extend(_day_rows)
    if _s35_rows:
        _s35_new = pd.DataFrame(_s35_rows)
        if _s35_old is not None:
            _s35_comb = pd.concat([_s35_old, _s35_new], ignore_index=True)
        else:
            _s35_comb = _s35_new
        _s35_tmp = S35 + '.tmp'
        _s35_comb.to_csv(_s35_tmp, index=False)
        os.replace(_s35_tmp, S35)
        print(f'35d结算: 新增{len(_s35_rows)}笔 ({_s35_new["date"].min()}~{_s35_new["date"].max()})')
    else:
        print('35d结算: 无新增')
except Exception as _e35:
    print(f'⚠ 35d结算失败({_e35}), 不影响全量结算')

# ═══ 异常汇总 (2026-09-11): 仅异常时输出到stdout (会被推送用户) ═══
try:
    _expect_min = (date.today() - timedelta(days=2)).isoformat()
    _hdf_now = pd.read_csv(HDF)
    _hdf_now['date'] = _hdf_now['date'].astype(str)
    _latest_h = _hdf_now['date'].max()
    if _latest_h < _expect_min:
        problem(f'复盘结算滞后: hourly_decisions 最新={_latest_h} < 期望≥{_expect_min}')
    if os.path.exists(ARCHIVE_DIR):
        _arch_dates = sorted({m.group(1) for fname in os.listdir(ARCHIVE_DIR)
                              for m in [ARCHIVE_RE.match(fname)] if m})
        _hdf_dates = set(_hdf_now['date'].unique())
        for _ad in _arch_dates:
            if _ad < _expect_min:
                if _ad not in price_by_date:
                    problem(f'价格缺失超窗: {_ad} (归档在/价格无, 上游发布或拉取异常)')
                elif _ad not in _hdf_dates:
                    problem(f'有价未入账: {_ad} (价格在库但未追加, 请检查)')
except Exception as _e_sum:
    problem(f'异常汇总检查失败: {_e_sum}')

if _PROBLEMS:
    _STDOUT.write('⚠️ v2_append_daily 异常:\n')
    for _p in _PROBLEMS:
        _STDOUT.write(f'  - {_p}\n')
    _STDOUT.flush()
try:
    _LOGF.close()
except Exception:
    pass
