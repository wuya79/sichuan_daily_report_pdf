#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四川发布时效探针（2026-09-15 一次性，只读）
采样 09:06-09:44 每 2 分钟: 老口 vs 新口(镜像) 对 date=09-15 的首现/更新时刻。
no_agent 用法: 结束时打印汇总(唯一 stdout)；中间过程静默。
"""
import contextlib
import hashlib
import io
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, '/home/ubuntu/sichuan_hydro_price')
import raydon_api as R  # noqa: E402

STATE = os.path.expanduser('~/.hermes/scripts/.probe_sc_timing_20260915.jsonl')
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
PX = 'https://data.raydon.com.cn/sc-px-dataanalysis/export/searchDynamicTableData'

CLASSES = [(1, 't1电价'), (19, 't19负荷'), (23, 't23发电'), (24, 't24水电'), (26, 't26非市场'), (27, 't27新能源')]
MIRRORS = [(10118, 't1电价'), (10109, 't19负荷'), (10110, 't23发电'), (10112, 't24水电'), (10111, 't26非市场'), (10108, 't27新能源')]


def md5v(vals):
    s = '|'.join(f'{v:.1f}' for v in vals)
    return hashlib.md5(s.encode()).hexdigest()[:10]


def old_sample(dt, ds):
    try:
        r = R.fetch_data(dt, ds, max_retries=1)
    except Exception:  # noqa: BLE001
        return 0, '', 0
    rows = (r or {}).get('data') or []
    pts = []
    for row in rows:
        for x in (row.get('data') or []):
            try:
                pts.append(float(x))
            except (TypeError, ValueError):
                pass
    return len(pts), (md5v(pts) if pts else ''), len(rows)


def new_sample(mid, ds):
    try:
        if mid <= 10128:
            url = f'https://data.raydon.com.cn/api/searchDynamicTableData/{mid}'
            body = {'sign': R.SIGN, 'hashMap': {'varNowDate': ds}, 'pageInfo': {'pageNum': 1, 'pageSize': 50}}
        else:
            url = f'{PX}/{mid}?sign={R.SIGN}'
            body = {'hashMap': {'varNowDate': ds}, 'pageInfo': {'pageNum': 1, 'pageSize': 50}}
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            j = json.loads(r.read().decode())
        rows = ((j.get('data') or {}) or {}).get('list') or []
        pts = []
        for row in rows:
            for k, v in row.items():
                if ':' in str(k):
                    try:
                        pts.append(float(str(v).replace(',', '').strip()))
                    except (TypeError, ValueError):
                        pass
        return len(pts), (md5v(pts) if pts else ''), len(rows)
    except Exception:  # noqa: BLE001
        return 0, '', 0


def take_batch(ts):
    out = []
    for dt, nm in CLASSES:
        n, h, rw = old_sample(dt, '2026-09-15')
        out.append({'ts': ts, 'k': f'{dt}|old', 'nm': nm, 'side': 'old', 'n': n, 'md5': h})
    for mid, nm in MIRRORS:
        n, h, rw = new_sample(mid, '2026-09-15')
        out.append({'ts': ts, 'k': f'{mid}|new', 'nm': nm, 'side': 'new', 'n': n, 'md5': h})
    n, h, rw = old_sample(19, '2026-09-16')
    out.append({'ts': ts, 'k': '19@0916|old', 'nm': 't19@09-16', 'side': 'old', 'n': n, 'md5': h})
    n, h, rw = new_sample(10109, '2026-09-16')
    out.append({'ts': ts, 'k': '19@0916|new', 'nm': 't19@09-16', 'side': 'new', 'n': n, 'md5': h})
    return out


def summarize(samples):
    if not samples:
        return '⏱ 探针 09-15：未采集到样本（异常）。'
    tss = sorted({s['ts'] for s in samples})
    order = []
    for s in samples:
        if s['k'] not in order:
            order.append(s['k'])
    by = {}
    for s in samples:
        by.setdefault(s['k'], {})[s['ts']] = s
    lines = ['⏱ 四川发布时效探针 2026-09-15（采样 09:06-09:44 每2分钟；数据=09-15 当日）']
    for k in order:
        d = by[k]
        nm = d[list(d.keys())[0]]['nm']
        side = '老口' if k.endswith('|old') else '新口'
        first, changes, last_n, last_h = None, [], 0, None
        for ts in tss:
            s = d.get(ts)
            if not s or s['n'] <= 0:
                continue
            if first is None:
                first = ts
            if s['md5'] and s['md5'] != last_h:
                changes.append(ts)
                last_h = s['md5']
            last_n = s['n']
        if first is None:
            lines.append(f'  {nm}·{side}: 至采样结束未出现')
        elif len(changes) <= 1:
            lines.append(f'  {nm}·{side}: 首现{first}｜首版即稳定｜末样{last_n}点')
        else:
            lines.append(f'  {nm}·{side}: 首现{first}｜共{len(changes)}版，最后变更{changes[-1]}｜末样{last_n}点')
    lines.append('（一次性探针，结束即自灭；原始样本已存档）')
    return '\n'.join(lines)


def main():
    now = datetime.now()
    if now.strftime('%Y-%m-%d') != '2026-09-15':
        print(f'探针跳过：日期不符 {now:%F %T}')
        return
    samples = []
    for minute_target in range(6, 45, 2):
        tg = now.replace(hour=9, minute=minute_target, second=5, microsecond=0)
        while datetime.now() < tg:
            time.sleep(5)
        ts = datetime.now().strftime('%H:%M')
        if ts > '09:45':
            break
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                batch = take_batch(ts)
        except Exception:  # noqa: BLE001
            batch = []
        if batch:
            samples.extend(batch)
            try:
                with open(STATE, 'a') as f:
                    for s in batch:
                        f.write(json.dumps(s, ensure_ascii=False) + '\n')
            except Exception:  # noqa: BLE001
                pass
    print(summarize(samples))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f'⏱ 探针异常：{type(e).__name__} {str(e)[:80]}')
