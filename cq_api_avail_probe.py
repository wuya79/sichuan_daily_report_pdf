#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重庆 API 可用时刻探针 — 测 D-1 实时出清 / D 日前价 / 交易通道 / OSS包 的最早可用时刻
静默运行：每次只追加一行 CSV，stdout 永远为空（no_agent 不打扰）。
"""
import csv, json, os, urllib.request, datetime

API = 'http://127.0.0.1:45678/api/query'
TRADE = 'http://127.0.0.1:45678/api/trade'
OSS = 'https://electricity-bill.oss-cn-chengdu.aliyuncs.com/chongqing'
OUT = '/tmp/cq_api_avail_probe.csv'
HDR = ['ts', 'sys_rt_rows', 'sys_rt_pts', 'sys_da_rows', 'sys_da_pts',
       'trade_rt_pts', 'oss_zip', 'oss_lm']


def q(dt, ds):
    """系统通道：返回 (rows, 电能量价格点数)"""
    try:
        req = urllib.request.Request(API, json.dumps({'data_type': dt, 'search_date': ds}).encode(),
                                     {'Content-Type': 'application/json'})
        r = json.loads(urllib.request.urlopen(req, timeout=20).read())
        rows = (r.get('data') or {}).get('rows') or []
        pts = 0
        for row in rows:
            if row.get('数据类型') == '电能量价格':
                pts = sum(1 for k, v in row.items()
                          if k.startswith('V') and v not in (None, '', '-'))
                break
        return len(rows), pts
    except Exception:
        return -1, -1


def q_trade_rt(ds):
    """交易通道 t12（实时出清结果）：返回点数"""
    try:
        req = urllib.request.Request(TRADE, json.dumps({'data_type': 12, 'info_date': ds}).encode(),
                                     {'Content-Type': 'application/json'})
        r = json.loads(urllib.request.urlopen(req, timeout=20).read())
        return sum(1 for x in (r.get('data') or [])
                   if str(x.get('info_type')) == '1' and x.get('info_value') not in (None, '', '-'))
    except Exception:
        return -1


def head(d):
    try:
        req = urllib.request.Request(f'{OSS}/{d}.zip', method='HEAD')
        with urllib.request.urlopen(req, timeout=15) as r:
            lm = r.headers.get('Last-Modified') or ''
            t = datetime.datetime.strptime(lm, '%a, %d %b %Y %H:%M:%S %Z') + datetime.timedelta(hours=8)
            return 200, t.strftime('%m-%d %H:%M:%S')
    except Exception:
        return 404, ''


def main():
    now = datetime.datetime.now()
    d1 = (now.date() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    d0 = now.date().strftime('%Y-%m-%d')
    rt_rows, rt_pts = q(2, d1)
    da_rows, da_pts = q(5, d0)
    tr_pts = q_trade_rt(d1)
    oss_st, oss_lm = head(now.strftime('%Y%m%d'))
    new = not os.path.exists(OUT)
    with open(OUT, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new:
            w.writerow(HDR)
        w.writerow([now.strftime('%m-%d %H:%M:%S'), rt_rows, rt_pts, da_rows, da_pts,
                    tr_pts, oss_st, oss_lm])


if __name__ == '__main__':
    main()
