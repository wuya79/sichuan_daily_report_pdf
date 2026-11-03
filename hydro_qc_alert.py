#!/usr/bin/env python3
"""水情质检告警（no_agent cron：健康时零输出，异常才输出一条）

时点：09:45 / 14:35
读取 /home/ubuntu/sichuan_hydro_price/logs/qc.jsonl 当日最新记录 →
分级（G1-G4）→ 6h 去重 → 微信/企微群推送。
"""
import json
import os
from datetime import datetime

HYDRO_DIR = '/home/ubuntu/sichuan_hydro_price'
QC_LOG = os.path.join(HYDRO_DIR, 'logs', 'qc.jsonl')
STATE_FILE = os.path.expanduser('~/.hermes/state/hydro_qc_state.json')
COOLDOWN_H = 6
MAX_PER_DAY = 3


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w') as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass


def latest_today():
    today = datetime.now().strftime('%Y-%m-%d')
    best = None
    try:
        with open(QC_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if str(e.get('ts', ''))[:10] != today:
                    continue
                if e.get('run') == 'test':
                    continue
                if best is None or str(e.get('ts')) > str(best.get('ts')):
                    best = e
    except Exception:
        return None
    return best


def evaluate(e):
    valid = int(e.get('valid') or 0)
    fresh = int(e.get('fresh') or 0)
    filtered = int(e.get('filtered') or 0)
    if valid < 40:
        return 'P0', f'valid{valid}'
    if valid < 46:
        return 'P1', f'valid{valid}'
    if fresh < 38:
        return 'P1', f'fresh{fresh}'
    if filtered > 20:
        return 'P1', f'filtered{filtered}'
    if filtered > 5:
        return 'P2', f'filtered{filtered}'
    return None, ''


def alert_text(e, level):
    ts = str(e.get('ts', ''))[:16].replace('T', ' ')
    gaps = e.get('gaps') or []
    gtxt = '、'.join(gaps[:5]) + (f' 等{len(gaps)}站' if len(gaps) > 5 else '')
    head = {'P0': '🔴 水情告警 P0', 'P1': '⚠️ 水情告警 P1', 'P2': 'ℹ️ 水情提示 P2'}[level]
    out = [f"{head} · {ts}"]
    out.append(f"有效站 {e.get('valid')}/{e.get('stations')}" + (f"（缺 {gtxt}）" if gtxt else ''))
    out.append(f"最新 {e.get('latest', '?')}｜新鲜 {e.get('fresh')}/{e.get('stations')}｜尖峰过滤 {e.get('filtered', 0)}")
    if e.get('source'):
        out.append(f"来源 {e.get('source')}")
    out.append('动作：' + ('需人工介入（检查 sign/联系邱总）' if level == 'P0' else '无需人工；连续2日不恢复升级 P0'))
    return '\n'.join(out)


def main():
    st = load_state()
    today = datetime.now().strftime('%Y-%m-%d')
    if st.get('date') != today:
        st = {'date': today, 'sent': {}, 'count': 0, 'last_level': None}

    e = latest_today()
    if e is None:
        # 当日无归档记录：09:45 后判定为未运行
        if datetime.now().hour >= 10:
            key = 'P1:noqc'
            if not st['sent'].get(key):
                print(f"⚠️ 水情告警 P1 · {datetime.now().strftime('%m-%d %H:%M')}\n"
                      f"今日无水情归档记录（qc.jsonl 无 {today} 条目）\n动作：检查 09:30 日报是否运行")
                st['sent'][key] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
                st['count'] = st.get('count', 0) + 1
                st['last_level'] = 'P1'
                save_state(st)
        return

    level, reason = evaluate(e)
    now = datetime.now()

    if level is None:
        if st.get('last_level') in ('P0', 'P1'):
            print(f"✅ 水情恢复 · {now.strftime('%m-%d %H:%M')}\n"
                  f"有效站 {e.get('valid')}/{e.get('stations')}，最新 {e.get('latest')}")
            st['last_level'] = None
            save_state(st)
        return

    key = f'{level}:{reason}'
    last = st['sent'].get(key)
    if last:
        try:
            elapsed = (now - datetime.strptime(last, '%Y-%m-%dT%H:%M:%S')).total_seconds() / 3600
            if elapsed < COOLDOWN_H:
                return
        except Exception:
            pass
    if st.get('count', 0) >= MAX_PER_DAY:
        return

    print(alert_text(e, level))
    st['sent'][key] = now.strftime('%Y-%m-%dT%H:%M:%S')
    st['count'] = st.get('count', 0) + 1
    st['last_level'] = level
    save_state(st)


if __name__ == '__main__':
    main()
