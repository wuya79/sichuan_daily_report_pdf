#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快照回填：修复 .daily_fc_snapshot.json 中因晨报取数失败留下的 None 字段。

背景（2026-09-15）：
- 日报每天 09:30 把当日预测（load/hydro/re/nim 的 avg_mw）写入快照，写一次、不重试。
- 若当晨某族（如 t24 水电）未发布 → 快照存 None → 次日"实际 vs 昨日预测"偏差算不出来
  （水电偏差/来水偏差齐灭，且重跑修不回）。09-12、09-14 均中招。
修复方式：扫描最近 N 天，对 None 字段用同日预测 wrapper（t19/t24/t27/t26）重取回填；
只回填、绝不覆盖已有值；原子写 + .bak_fix 备份；无命中静默（供 cron 留痕用）。

用法: python3 fix_fc_snapshot.py [--days 14] [--dry-run]
"""
import argparse
import datetime
import json
import os
import shutil
import sys

PROJ = '/home/ubuntu/sichuan_hydro_price'
SNAP = os.path.join(PROJ, '.daily_fc_snapshot.json')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=14, help='扫描最近 N 天（default 14）')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    sys.path.insert(0, PROJ)
    try:
        import raydon_api
    except Exception as e:
        print(f'⚠️ 快照回填: raydon_api 导入失败: {e}')
        return 1
    try:
        with open(SNAP, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f'⚠️ 快照回填: 快照读取失败: {e}')
        return 1
    if not isinstance(data, dict):
        print('⚠️ 快照回填: 快照结构异常（非 dict）')
        return 1

    today = datetime.date.today()
    eligible = {(today - datetime.timedelta(days=i)).isoformat()
                for i in range(0, args.days + 1)}
    fetchers = {
        'load': raydon_api.get_load_forecast,       # t19
        'hydro': raydon_api.get_hydro_forecast,     # t24
        're': raydon_api.get_renewable_forecast,    # t27
        'nim': raydon_api.get_nim_forecast,         # t26
    }
    fixes = []
    for d in sorted(data.keys()):
        if d not in eligible:
            continue
        rec = data.get(d)
        if not isinstance(rec, dict):
            continue
        for field, fn in fetchers.items():
            if rec.get(field) is not None:
                continue
            try:
                r = fn(d)
            except Exception:
                r = None
            v = r.get('avg_mw') if isinstance(r, dict) else None
            if not (isinstance(v, (int, float)) and v > 0):
                v = None
            if v is not None:
                rec[field] = float(v)
                fixes.append(f'{d}.{field}={float(v):.1f}')

    if not fixes:
        return 0  # 无命中：静默（cron 模式不打扰）
    if args.dry_run:
        print('（dry-run）将回填: ' + '; '.join(fixes))
        return 0
    try:
        shutil.copyfile(SNAP, SNAP + '.bak_fix')
        tmp = SNAP + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, SNAP)
    except Exception as e:
        print(f'⚠️ 快照回填: 写入失败: {e}')
        return 1
    print('✅ 快照回填: ' + '; '.join(fixes))
    return 0


if __name__ == '__main__':
    sys.exit(main())
