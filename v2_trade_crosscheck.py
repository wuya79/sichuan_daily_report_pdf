#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重庆V2 交易接口交叉校验（影子期工具，2026-09-10 起观察1周）

用途：每日静默核验「系统接口 vs 交易接口」在 4 类实际值 + 实时价上的等价性，
      记录"系统缺失→可兜底"的日子，为 trade_fallback 上线提供证据。

设计：
- 默认扫描最近4天（D-1~D-4，捕捉迟到补发布）；--date 指定单日
- 无异常时零输出（cron no_agent 模式=静默）；有异常时输出到 stdout（会被投递）
- 状态累积：/home/ubuntu/v2_cq_strategy/output/trade_crosscheck_state.json
- 阈值：实际值 diff>0.01 告警；rt max差>5.0 告警（正常日已知≤2.3）
- 用法: v2_trade_crosscheck.py [--date 2026-09-09] [--verbose] [--report]

口径与证据：skill chongqing-power-data → references/chongqing-api-official-doc-verified.md
"""
import json
import os
import re
import sys
import urllib.request
from datetime import date as _date, datetime, timedelta

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

BASE = '/home/ubuntu/v2_cq_strategy'
STATE = f'{BASE}/output/trade_crosscheck_state.json'
API = 'http://127.0.0.1:45678/api/query'
TRADE = 'http://127.0.0.1:45678/api/trade'
VRE = re.compile(r'^[Vv](\d{4})')
ACTUAL_MAP = {"gen": (1, 6), "load": (3, 8), "hydro": (6, 9), "nonmkt": (8, 7)}
RT_MAX_ALERT = 5.0      # rt 双源 max 差告警阈值（正常日 ≤2.3）
ACT_DIFF_ALERT = 0.01   # 实际值逐点容差


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def _vpoints(row):
    out = {}
    for k, v in row.items():
        m = VRE.match(str(k))
        if m and v not in (None, "", "-"):
            try:
                out[(int(m.group(1)) // 100) * 60 + (int(m.group(1)) % 100)] = float(str(v).replace(',', ''))
            except Exception:
                pass
    return out


def system_data(dt, ds, label='电能量价格'):
    """返回 (points, raw_rows, err)。types 2/5 按'数据类型'行名匹配；其余取首行。"""
    try:
        r = _post(API, {"data_type": dt, "search_date": ds})
    except Exception as e:
        return {}, None, f"fetch_error:{type(e).__name__}"
    rows = (r.get("data") or {}).get("rows", []) or []
    if dt in (2, 5):
        for x in rows:
            if str(x.get("数据类型", "")) == label:
                return _vpoints(x), rows, ""
    elif rows:
        return _vpoints(rows[0]), rows, ""
    return {}, rows, ""


def trade_actual(td, ds):
    try:
        r = _post(TRADE, {"data_type": td, "info_date": ds})
    except Exception as e:
        return {}, f"fetch_error:{type(e).__name__}"
    d = r.get("data")
    if isinstance(d, list) and d and isinstance(d[0], dict):
        return _vpoints(d[0]), ""
    return {}, ""


def trade_t12_rt(ds):
    try:
        r = _post(TRADE, {"data_type": 12, "info_date": ds})
    except Exception as e:
        return {}, f"fetch_error:{type(e).__name__}"
    out = {}
    for x in r.get("data", []) or []:
        if str(x.get("info_type")) == "1":
            try:
                h, m = str(x.get("info_time")).split(':')
                out[int(h) * 60 + int(m)] = float(x.get("info_value"))
            except Exception:
                pass
    return out, ""


def check_day(ds):
    res = {"checked_at": datetime.now().isoformat(timespec='seconds'), "items": {}, "alerts": []}
    errs = []
    for name, (sd, td) in ACTUAL_MAP.items():
        a, _, ea = system_data(sd, ds)
        b, eb = trade_actual(td, ds)
        if ea:
            errs.append(f"{name}:{ea}")
        if eb:
            errs.append(f"{name}:{eb}")
        if a and b:
            common = set(a) & set(b)
            nd = sum(1 for m in common if abs(a[m] - b[m]) > ACT_DIFF_ALERT)
            mx = max((abs(a[m] - b[m]) for m in common), default=None)
            if nd:
                res["items"][name] = f"DIFF({nd}点,max={mx:.2f})"
                res["alerts"].append(f"{ds} {name}: 系统vs交易 {nd}点不一致 max={mx:.4f}")
            else:
                res["items"][name] = f"ok({len(common)}点,0差)"
        elif a and not b:
            res["items"][name] = "trade缺"
        elif b and not a:
            res["items"][name] = "sys缺(可兜)"
        else:
            res["items"][name] = "双缺"
    # rt：系统 t2（电能量价格行）vs 交易 t12（info_type=1）
    a, rows_a, ea = system_data(2, ds)
    b, eb = trade_t12_rt(ds)
    if ea:
        errs.append(f"rt:{ea}")
    if eb:
        errs.append(f"rt:{eb}")
    if a and b:
        common = set(a) & set(b)
        mx = max((abs(a[m] - b[m]) for m in common), default=0.0)
        res["items"]["rt"] = f"max={mx:.2f}"
        if mx > RT_MAX_ALERT:
            res["alerts"].append(f"{ds} rt: 系统vs交易 max={mx:.2f} 超阈值{RT_MAX_ALERT}")
    elif b and not a:
        res["items"]["rt"] = "sys残→可兜" if rows_a else "sys缺(可兜)"
    elif a and not b:
        res["items"]["rt"] = "trade缺"
    else:
        res["items"]["rt"] = "双缺"
    if errs:
        res["alerts"].append(f"{ds} 取数异常: {', '.join(sorted(set(errs)))}")
    return res


def main():
    args = sys.argv[1:]
    verbose = '--verbose' in args
    report = '--report' in args
    d = None
    if '--date' in args:
        d = args[args.index('--date') + 1]

    state = {"last_run": "", "days": {}}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE, encoding='utf-8'))
        except Exception:
            pass
    state.setdefault("days", {})

    if report:
        print("== 交易接口交叉校验 状态（近14天）==")
        print("date       | gen | load | hydro | nonmkt | rt")
        for k in sorted(state["days"])[-14:]:
            it = state["days"][k]["items"]
            print(f"{k} | " + " | ".join(it.get(x, "-") for x in ("gen", "load", "hydro", "nonmkt", "rt")))
        return

    today = _date.today()
    dates = [d] if d else [(today - timedelta(days=i)).isoformat() for i in range(1, 5)]

    new_alerts = []
    for ds in dates:
        res = check_day(ds)
        old = state["days"].get(ds, {})
        old_alerts = old.get("alerts", [])
        for a in res["alerts"]:
            if a not in old_alerts:
                new_alerts.append(a)
        merged = dict(old)
        merged["checked_at"] = res["checked_at"]
        merged["items"] = res["items"]
        merged["alerts"] = sorted(set(old_alerts) | set(res["alerts"]))
        state["days"][ds] = merged
        if verbose:
            print(f"[{ds}] " + " | ".join(f"{k}={v}" for k, v in res["items"].items()))
            for a in res["alerts"]:
                print(f"    ⚠ {a}")

    state["last_run"] = datetime.now().isoformat(timespec='seconds')
    keep = sorted(state["days"])[-60:]
    state["days"] = {k: state["days"][k] for k in keep}
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)

    if new_alerts:
        print("⚠️ 交易接口交叉校验告警：")
        for a in new_alerts:
            print(f"- {a}")
        print("(详见 output/trade_crosscheck_state.json)")


if __name__ == '__main__':
    main()
