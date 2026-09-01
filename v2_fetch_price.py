#!/usr/bin/env python3
"""价格库补拉 — 09:24 cron（2026-08-15 审计后新增）
职责：把 cq_price_history.json 补齐到"昨天"（今天rt未走完，不建今天行）。
在 stage2(09:26) 特征重建之前运行，保证特征重建能建出最新完整日。
幂等：已存在且rt完整的日期跳过。原子写 + 写前备份。
"""
import json
import os
import re
import shutil
import urllib.request
from datetime import date, timedelta

PH = '/home/ubuntu/v2_cq_strategy/output/cq_price_history.json'
API_URL = 'http://127.0.0.1:45678/api/query'
V_RE = re.compile(r'^V(\d{4})')


def _aggregate_to_hourly(raw):
    hh = {}
    for k, v in raw.items():
        minute = int(k[:2]) * 60 + int(k[2:])
        h = max(0, (minute - 1) // 60)
        hh.setdefault(h, []).append(v)
    return [sum(hh.get(h, [0])) / len(hh[h]) if h in hh else None for h in range(24)]


def _is_complete(rec):
    """24小时da/rt全有值且不全为0才算完整（防partial/全0污染特征）"""
    for key in ('da', 'rt'):
        arr = rec.get(key) or []
        if len(arr) != 24 or any(v is None for v in arr):
            return False
        if all(v == 0 for v in arr):
            # 2026-09-02守卫: 不再静默——0元地板价真实存在(2026-08-30实测), 提示人工确认
            print(f"⚠ {rec.get('date')} {key}全天为0: 疑似数据污染或极端地板价, 跳过写入, 请人工确认")
            return False
    return True


def _fetch_day(ds):
    da_raw, rt_raw = {}, {}
    for tid, store in ((5, da_raw), (2, rt_raw)):
        req = urllib.request.Request(
            API_URL,
            json.dumps({"data_type": tid, "search_date": ds}).encode(),
            {"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        for row in r.get("data", {}).get("rows", []):
            if row.get("数据类型") == "电能量价格":
                for k, v in row.items():
                    m = V_RE.match(k)
                    if m and v not in (None, "", "-"):
                        store[m.group(1)] = float(str(v).replace(",", ""))
                break
    da_list = _aggregate_to_hourly(da_raw)
    rt_list = _aggregate_to_hourly(rt_raw)
    if not da_raw and not rt_raw:
        # 2026-09-02守卫: 上游字段名变化(如V0005→V0005出清节点电价)时正则静默拉空
        print(f"⚠ {ds}: 上游V字段未匹配(字段名可能变化), da/rt原始点均为空, 请检查API返回结构")
    if any(v is not None for v in da_list) and any(v is not None for v in rt_list):
        return {"date": ds, "da": da_list, "rt": rt_list}
    return None


def main():
    with open(PH) as f:
        ph = json.load(f)
    by_date = {d['date']: d for d in ph}
    existing_dates = sorted(by_date.keys())
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    if not existing_dates:
        print("⚠ 价格库为空，无法确定补拉起点，退出")
        return 1

    if existing_dates and existing_dates[-1] >= yesterday:
        # 已到昨天：只检查最近3天完整性，partial/全0记录重拉
        targets = [ds for ds in existing_dates[-3:] if not _is_complete(by_date[ds])]
    else:
        # 从最新日期+1补到昨天
        start = date.fromisoformat(existing_dates[-1]) + timedelta(days=1)
        targets = []
        d = start
        while d.isoformat() <= yesterday:
            targets.append(d.isoformat())
            d += timedelta(days=1)

    if not targets:
        print("价格库已最新（到昨天），无需补拉")
        return 0

    print(f"补拉 {len(targets)} 天: {targets[0]}~{targets[-1]}")
    changed = False
    for ds in targets:
        try:
            rec = _fetch_day(ds)
            if rec and _is_complete(rec):
                if ds in by_date:
                    idx = next(i for i, x in enumerate(ph) if x['date'] == ds)
                    ph[idx] = rec          # 替换不完整记录
                else:
                    ph.append(rec)
                changed = True
                print(f"  ✅ {ds}")
            else:
                print(f"  ⚠ {ds}: 数据不完整(rt未走完或上游未发布)，跳过")
        except Exception as e:
            print(f"  ⚠ {ds}: 拉取失败({e})")
    if changed:
        ph.sort(key=lambda x: x['date'])
        # 写前备份（与append的HDF.bak先例一致）
        if os.path.exists(PH):
            shutil.copy2(PH, PH + '.bak')
        tmp = PH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(ph, f)
        os.replace(tmp, PH)
        print("已备份+原子写回价格库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
