#!/usr/bin/env python3
"""OSS 重庆数据包发布探测 — 状态文件去重,发现新包才输出(no_agent watchdog模式)
探测: WATCH列表中的数据包(观察发布时刻:当天出或延迟到几点)
状态: ~/.hermes/scripts/oss_probe_state.json 记录已通知日期
"""
import json, os, time, urllib.request, urllib.error, datetime

BASE = 'https://electricity-bill.oss-cn-chengdu.aliyuncs.com/chongqing'
STATE = os.path.expanduser('~/.hermes/scripts/oss_probe_state.json')
WATCH = ['20260910', '20260911']  # 今日包 + 明日包(观察提前量)

def check(d):
    url = f'{BASE}/{d}.zip'
    try:
        req = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(req, timeout=15) as r:
            size = r.headers.get('Content-Length', '?')
            return r.status, size
    except urllib.error.HTTPError as e:
        return e.code, 0
    except Exception as e:
        return None, str(e)[:40]

def main():
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    msgs = []
    for d in WATCH:
        st, size = check(d)
        prev = state.get(d, '404')
        if st == 200 and prev != '200':
            msgs.append(f'✅ OSS {d}.zip 已出现! {now} | 大小 {size}B | 探测时点: 从{prev}→200')
            state[d] = '200'
        elif st != 200:
            state[d] = str(st) if st else 'ERR'
    json.dump(state, open(STATE, 'w'))
    if msgs:
        print('\n'.join(msgs))
    # 无新包则静默(no_agent空stdout=不打扰)

if __name__ == '__main__':
    main()
