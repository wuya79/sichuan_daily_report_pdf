#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""水情恢复哨兵（2026-10-16）—— 省平台恢复后：自动预热 + 自动回填冻结缺口

背景：省平台/备用接口双断（10-10起），日报水情逐日冻结。省平台恢复后需要
     (a) 刷新缓存（预热）(b) 把缺口窗口真值回填 历史库/趋势库。
     cron 每30分钟：未恢复→静默；恢复且有缺口→自动执行并输出摘要。

流程：
 1) 探测4个代表站宽窗口；成功站数<3 → 静默退出。
 2) 缓存龄>2天站数==0 → 静默退出。
 3) 预热刷新缓存。
 4) 回填 [D-10, D-1]：对站s/日期d取"d日00:00前最后一条记录"（复刻日报晨取数语义），
    仅实质差异(水位差>0.15m 或 蓄量差>10)替换；history+trends；备份+原子写。
 5) 有写入才输出摘要；恢复但无进展且与上次消息相同 → 静默。

安全：09:20~09:50 生产窗口不动作（防与日报读改写冲突）。
"""
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

PROJ = '/home/ubuntu/sichuan_hydro_price'
RH = os.path.join(PROJ, '.reservoir_history.json')
DT = os.path.join(PROJ, '.daily_trends.json')
CACHE = os.path.join(PROJ, '.reservoir_cache.json')
STATE = '/home/ubuntu/.hermes/state/reservoir_sentinel_state.json'
PREHEAT = '/home/ubuntu/.hermes/scripts/sichuan_reservoir_preheat.py'
sys.path.insert(0, PROJ)
import sichuan_hydro_api as sh  # noqa: E402

PROBE_NAMES = ['瀑布沟', '二滩', '紫坪铺', '龚嘴']
STALE_DAYS = 2.0
WIN_DAYS = 10


def _now():
    return datetime.now()


def _fnum(v):
    try:
        return float(v)
    except Exception:
        return None


def probe():
    """(成功站数, 站总数)；成功=有记录且最新sj >= D-2"""
    d_lo = (date.today() - timedelta(days=7)).isoformat()
    d_hi = date.today().isoformat()
    thr = (date.today() - timedelta(days=2)).isoformat()
    tgt = [e for e in sh.KEY_RESERVOIRS if e[0] in PROBE_NAMES]

    def one(entry):
        for _att in range(2):
            try:
                recs = sh.get_reservoir_data(entry[1], start_time=d_lo, end_time=d_hi,
                                             max_rows=1200, timeout=(6, 8))
                if recs:
                    sj = max(str(r.get('sj') or '') for r in recs)
                    if sj[:10] >= thr:
                        return True
            except Exception:
                pass
            time.sleep(0.4)
        return False

    ok = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for fut in as_completed([ex.submit(one, e) for e in tgt]):
            if fut.result():
                ok += 1
    return ok, len(tgt)


def stale_count():
    try:
        c = json.load(open(CACHE, encoding='utf-8'))
        n = 0
        for r in c:
            ts = str(r.get('时间') or '')[:19]
            try:
                age = (_now() - datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')).total_seconds() / 86400
            except Exception:
                age = 0
            if age > STALE_DAYS:
                n += 1
        return n
    except Exception:
        return -1


def load_state():
    try:
        return json.load(open(STATE, encoding='utf-8'))
    except Exception:
        return {}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump(st, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    except Exception:
        pass


def fetch_all():
    d_lo = (date.today() - timedelta(days=WIN_DAYS + 1)).isoformat()
    d_hi = date.today().isoformat()

    def one(entry):
        for _att in range(6):
            try:
                recs = sh.get_reservoir_data(entry[1], start_time=d_lo, end_time=d_hi,
                                             max_rows=2000, timeout=(6, 8))
                if recs:
                    return entry[0], recs
            except Exception:
                pass
            time.sleep(0.6)
        return entry[0], None

    raw = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        for fut in as_completed([ex.submit(one, e) for e in sh.KEY_RESERVOIRS]):
            n, recs = fut.result()
            raw[n] = recs
    return raw


def value_for_day(recs, d):
    """d日00:00前最后一条有实质数值记录"""
    best, best_sj = None, ''
    limit = d.isoformat()
    for r in recs:
        sj = str(r.get('sj') or '')
        day = sj[:10]
        if day and day < limit and sj > best_sj:
            if r.get('ksw') not in (None, '') or r.get('xsl') not in (None, ''):
                best, best_sj = r, sj
    return best


def backfill(raw):
    """返回 (history改写数, trends改写数, 备份戳|None)"""
    rh = json.load(open(RH, encoding='utf-8'))
    dtj = json.load(open(DT, encoding='utf-8'))
    rd = dtj.get('reservoir_daily', {})
    D = date.today()
    days = [D - timedelta(days=i) for i in range(1, WIN_DAYS + 1)]

    plan_rh, plan_dt = [], []
    for e in sh.KEY_RESERVOIRS:
        name = e[0]
        recs = raw.get(name)
        if not recs:
            continue
        for d in days:
            v = value_for_day(recs, d)
            if v is None:
                continue
            ds = d.isoformat()
            n_ksw, n_xsl = _fnum(v.get('ksw')), _fnum(v.get('xsl'))
            ent = rh.get(ds, {}).get(name)
            o_ksw = _fnum(ent.get('ksw')) if ent else None
            o_xsl = _fnum(ent.get('xsl')) if ent else None
            material = (ent is None or o_ksw is None or n_ksw is None
                        or abs(o_ksw - n_ksw) > 0.15
                        or (o_xsl is not None and n_xsl is not None and abs(o_xsl - n_xsl) > 10.0))
            if material:
                plan_rh.append((ds, name))
                plan_dt.append((ds, name))

    if not plan_rh and not plan_dt:
        return 0, 0, None

    stamp = _now().strftime('%Y%m%d_%H%M')
    shutil.copyfile(RH, RH + f'.bak_{stamp}')
    shutil.copyfile(DT, DT + f'.bak_{stamp}')
    n_rh = n_dt = 0
    for ds, name in plan_rh:
        v = value_for_day(raw[name], date.fromisoformat(ds))
        ent = rh.setdefault(ds, {}).setdefault(name, {})
        if v.get('ksw') is not None:
            ent['ksw'] = str(v.get('ksw'))
        if v.get('xsl') is not None:
            ent['xsl'] = str(v.get('xsl'))
        if v.get('rkll') is not None:
            ent['rkll'] = str(v.get('rkll'))
        if v.get('ckll') is not None:
            ent['ckll'] = str(v.get('ckll'))
        n_rh += 1
    for ds, name in plan_dt:
        v = value_for_day(raw[name], date.fromisoformat(ds))
        t = rd.setdefault(ds, {}).setdefault(name, {})
        if _fnum(v.get('ksw')) is not None:
            t['库水位'] = _fnum(v.get('ksw'))
        if _fnum(v.get('xsl')) is not None:
            t['蓄水量'] = _fnum(v.get('xsl'))
        if _fnum(v.get('rkll')) is not None:
            t['入库流量'] = _fnum(v.get('rkll'))
        if _fnum(v.get('ckll')) is not None:
            t['出库流量'] = _fnum(v.get('ckll'))
        n_dt += 1
    tmp = RH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(rh, f, ensure_ascii=False)
    os.replace(tmp, RH)
    tmp2 = DT + '.tmp'
    with open(tmp2, 'w', encoding='utf-8') as f:
        json.dump(dtj, f, ensure_ascii=False)
    os.replace(tmp2, DT)
    # 复读抽检（首条改写项）
    try:
        chk = json.load(open(RH, encoding='utf-8'))
        ds0, n0 = plan_rh[0]
        if chk.get(ds0, {}).get(n0, {}).get('ksw') in (None, ''):
            n_rh = -1  # 校验失败标记
    except Exception:
        n_rh = -2
    return n_rh, n_dt, stamp


def main():
    DRY = '--dry' in sys.argv
    now = _now()
    hm = now.hour * 60 + now.minute
    if 9 * 60 + 20 <= hm <= 9 * 60 + 50:
        if DRY:
            print('[dry] 生产窗口内，跳过')
        return 0  # 生产窗口：不动作

    ok, total = probe()
    st = load_state()
    st['last_probe'] = now.isoformat()
    st['last_probe_recovered'] = f'{ok}/{total}'
    if ok < 3:
        save_state(st)
        if DRY:
            print(f'[dry] probe={ok}/{total} → 未恢复，静默退出（符合预期）')
        return 0  # 未恢复：静默

    n_stale = stale_count()
    if n_stale <= 0:
        st['last_result'] = 'no-stale'
        save_state(st)
        if DRY:
            print(f'[dry] probe={ok}/{total} stale=0 → 无需动作（符合预期）')
        return 0

    if DRY:
        print(f'[dry] probe={ok}/{total} 缓存龄>2天={n_stale}站 → 【会触发】预热+回填（dry未执行）')
        return 0

    # 预热（刷新缓存）
    try:
        subprocess.run([sys.executable, PREHEAT], capture_output=True, timeout=600)
    except Exception:
        pass

    # 回填
    raw = fetch_all()
    ok2 = sum(1 for v in raw.values() if v)
    n_rh, n_dt, stamp = backfill(raw)
    st['last_full_attempt'] = now.isoformat()
    st['last_write_counts'] = [n_rh, n_dt]
    stale_after = stale_count()

    if n_rh > 0 or n_dt > 0:
        msg = (f'✅ 水情恢复哨兵: 省平台已恢复({ok}/{total}站探测, {ok2}/48站取数) — '
               f'已预热+回填近{WIN_DAYS}天窗口: history {n_rh}条 / trends {n_dt}条 '
               f'(备份.bak_{stamp})')
        print(msg)
        st['last_msg'] = msg
    elif stale_after > 0 and ok2 >= 10:
        msg = (f'⚠️ 水情恢复哨兵: 平台有响应({ok2}/48站)但 {stale_after} 站龄仍>2天 — '
               f'缺口日期平台暂无记录，待续回填')
        if st.get('last_msg') != msg:
            print(msg)
            st['last_msg'] = msg
    save_state(st)
    return 0


if __name__ == '__main__':
    sys.exit(main())
