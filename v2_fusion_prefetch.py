#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_fusion_prefetch — 重庆V2 双源融合前置组件 (2026-09-11)

职责: 每日(09:12 cron)从两个接口取数 → 校验/合并(互为兜底+点级拼接) → 写入既有兜底缓存
      data/supply_fallback.json (schema 兼容; 主链零改动)。
依据: 《重庆V2_双源融合取数层设计_20260911_V1.1》
原则:
  - 源顺序按配置(默认 系统→交易→zip); 交易只认"上午"行; 单字段单源; 不劣化既有条目
  - A类(已证全等字段)同颗粒度可点级拼接; 同点不一致 → 记 conflicts
  - 静默成功(日志入 output/fusion_prefetch.log); 目标日缺口/冲突 → stdout(cron告警投递)
用法: python3 v2_fusion_prefetch.py [--dry] [--sys-url URL] [--trade-url URL]
"""
import sys, os, json, shutil, urllib.request
from datetime import date, timedelta, datetime

BASE = '/home/ubuntu/v2_cq_strategy'
FB_PATH = os.path.join(BASE, 'data', 'supply_fallback.json')
CFG_PATH = os.path.join(BASE, 'v2_config.json')
LOG_PATH = os.path.join(BASE, 'output', 'fusion_prefetch.log')
SYS_URL = 'http://127.0.0.1:45678/api/query'
TRADE_URL = 'http://127.0.0.1:45678/api/trade'
import re
VRE = re.compile(r'^[Vv](\d{4})')

_argv = sys.argv
DRY = '--dry' in _argv
if '--sys-url' in _argv:   SYS_URL = _argv[_argv.index('--sys-url') + 1]
if '--trade-url' in _argv: TRADE_URL = _argv[_argv.index('--trade-url') + 1]
if '--fb' in _argv:        FB_PATH = _argv[_argv.index('--fb') + 1]  # 测试用: 指定兜底缓存文件

# (字段, 系统type, 交易type, 备注) — 预测类; gen 走单独分支(直取/推导)
FC_FIELDS = [
    ('load',     13, 3,    None),
    ('nonmkt',   14, 2,    None),
    ('tie_line', 12, 4,    None),
    ('hydro',    11, None, None),
    ('solar',    10, None, '光伏'),
    ('wind',     10, None, '风电'),
]
ACT_FIELDS = [
    ('act_gen',       1, 6),
    ('act_load',      3, 8),
    ('act_hydro',     6, 9),
    ('act_nonmkt',    8, 7),
    ('act_tieline',   7, None),
    ('act_newenergy', 4, None),
]

def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a') as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass

def _post(url, body, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def vpts(row):
    out = {}
    for k, v in row.items():
        m = VRE.match(str(k))
        if m and v not in (None, '', '-'):
            try:
                out[(int(m.group(1)) // 100) * 60 + (int(m.group(1)) % 100)] = float(str(v).replace(',', ''))
            except Exception:
                pass
    return out

def list_to_pts(arr):
    """fb 数组(96/288) → {minute: val}"""
    n = len(arr)
    step = 15 if n <= 100 else 5
    return {step * (i + 1): float(v) for i, v in enumerate(arr)}

def try2(fn, n=2):
    err = None
    for _ in range(n):
        try:
            return fn(), None
        except Exception as e:
            err = e
    return None, err

def sys_fetch(dt, ds, tag=None):
    def go():
        r = _post(SYS_URL, {"data_type": dt, "search_date": ds})
        rows = (r.get('data') or {}).get('rows', []) or []
        if tag:
            for row in rows:
                t = str(row.get('类型', '') or row.get('数据类型', ''))
                if tag in t:
                    p = vpts(row)
                    return p if p else None
            return None
        return vpts(rows[0]) if rows else None
    pts, err = try2(go, 2)
    if err:
        log(f"sys t{dt}@{ds} 调用异常: {err}")
    return pts

def trade_fetch(dt, ds, am_only=False):
    def go():
        r = _post(TRADE_URL, {"data_type": dt, "info_date": ds})
        d = r.get('data')
        if not isinstance(d, list) or not d:
            return None
        rows = [x for x in d if isinstance(x, dict)]
        if am_only:
            _f = [x for x in rows if '上午' in str(x.get('sjlx', ''))]
            if not _f and rows:
                log(f"trade t{dt}@{ds}: 有{len(rows)}行但无'上午'标签 → 跳过该源(需人工复核, 防误用下午行)")
            rows = _f
        for x in rows[:1]:
            p = vpts(x)
            if p:
                return p
        return None
    pts, err = try2(go, 2)
    if err:
        log(f"trade t{dt}@{ds} 调用异常: {err}")
    return pts

def valid(pts):
    """有效性: 90~100(15min) / 280~292(5min) 且 非全零"""
    if not pts:
        return False
    n = len(pts)
    if not (90 <= n <= 100 or 280 <= n <= 292):
        return False
    if all(v == 0 for v in pts.values()):
        return False
    return True

def complete_list(arr):
    if not arr:
        return False
    n = len(arr)
    return (90 <= n <= 100) or (280 <= n <= 292)

def to_list(pts):
    return [round(pts[k], 2) for k in sorted(pts)]

def load_cfg():
    try:
        with open(CFG_PATH) as f:
            cfg = json.load(f)
        return cfg.get('data_sources', {})
    except Exception:
        return {}

def main():
    t0 = datetime.now()
    cfg = load_cfg()
    order_fc = cfg.get('order', {}).get('supply_forecast', ['system', 'trade', 'zip'])
    order_act = cfg.get('order', {}).get('supply_actual', ['system', 'trade', 'zip'])
    offsets = cfg.get('coverage_dates_offset', [-2, -1, 0, 1])

    fb = {}
    if os.path.exists(FB_PATH):
        with open(FB_PATH) as f:
            fb = json.load(f)
    meta = fb.setdefault('_meta', {})
    today = date.today()
    report = {'writes': [], 'conflicts': [], 'revised': [], 'identity': []}

    def pick_and_merge(day, ds, field, cands, commit=True):
        """同颗粒度→点级并集; 颗粒度不一致→取优先级最高完整候选; 返回 (merged, srcmap) 或 None"""
        if not cands:
            return None
        gc = {'96' if len(p) <= 100 else '288' for _, p in cands}
        if len(gc) == 1:
            merged, srcmap = {}, {}
            for s, p in cands:
                for k, v in p.items():
                    if k not in merged:
                        merged[k] = v
                        srcmap[k] = s
                    elif abs(merged[k] - v) > 0.01:
                        report['conflicts'].append((ds, field, s, k, round(merged[k], 2), round(v, 2)))
            if not valid(merged):
                merged = None
        else:
            merged = None
        if merged is None:
            best = next(((s, p) for s, p in cands if complete_list(to_list(p))), None)
            if best is None:
                return None
            merged, srcmap = best[1], {k: best[0] for k in best[1]}
        return merged, srcmap

    def commit_field(day, ds, field, pts, srcmap):
        arr = to_list(pts)
        old = day.get(field)
        if complete_list(old):
            if len(old) == len(arr) and any(abs(a - b) > 0.5 for a, b in zip(old, arr)):
                report['revised'].append((ds, field))
            return False
        day[field] = arr
        srcs = '+'.join(sorted({srcmap[k] for k in pts}))
        report['writes'].append((ds, field, srcs))
        return True

    def candidates(day, ds, field, sdt, tdt, note, order, am_only=True):
        cands = []
        for src in order:
            if src == 'system' and sdt:
                p = sys_fetch(sdt, ds, tag=note if note in ('光伏', '风电') else None)
                if valid(p):
                    cands.append(('system', p))
            elif src == 'trade' and tdt:
                p = trade_fetch(tdt, ds, am_only=am_only)
                if valid(p):
                    cands.append(('trade', p))
            elif src == 'zip':
                z = day.get(field)
                if complete_list(z):
                    zp = list_to_pts(z)
                    if valid(zp):
                        cands.append(('zip', zp))
        return cands

    for off in offsets:
        ds = (today + timedelta(days=off)).isoformat()
        day = fb.setdefault(ds, {})
        chosen = {}

        # ---- 预测类 ----
        for field, sdt, tdt, note in FC_FIELDS:
            cands = candidates(day, ds, field, sdt, tdt, note, order_fc)
            r = pick_and_merge(day, ds, field, cands)
            if r is None:
                continue
            merged, srcmap = r
            chosen[field] = merged
            commit_field(day, ds, field, merged, srcmap)

        # ---- gen: 系统直取优先, 缺失→load-tie_line 推导 ----
        genp = None
        gen_src = 'system'
        if 'system' in order_fc:
            gp = sys_fetch(9, ds)
            if valid(gp):
                genp = gp
        if genp is None and chosen.get('load') and chosen.get('tie_line'):
            common = set(chosen['load']) & set(chosen['tie_line'])
            if len(common) >= 90:
                der = {k: chosen['load'][k] - chosen['tie_line'][k] for k in sorted(common)}
                if valid(der):
                    genp = der
                    gen_src = 'derived'
        if genp is not None:
            if chosen.get('load') and chosen.get('tie_line'):
                common2 = set(genp) & set(chosen['load']) & set(chosen['tie_line'])
                if common2:
                    mx = max(abs(genp[k] - (chosen['load'][k] - chosen['tie_line'][k])) for k in common2)
                    if mx > 1.0:
                        report['identity'].append((ds, round(mx, 1)))
            commit_field(day, ds, 'gen', genp, {k: gen_src for k in genp})

        # ---- 实际类 ----
        for field, sdt, tdt in ACT_FIELDS:
            cands = candidates(day, ds, field, sdt, tdt, None, order_act, am_only=False)
            r = pick_and_merge(day, ds, field, cands)
            if r is None:
                continue
            merged, srcmap = r
            commit_field(day, ds, field, merged, srcmap)

    # ---- 写入 ----
    if report['writes'] and not DRY:
        try:
            shutil.copy2(FB_PATH, FB_PATH + '.bak')
        except Exception:
            pass
        meta['fusion_prefetch'] = {
            'at': datetime.now().isoformat(timespec='seconds'),
            'writes': len(report['writes']),
        }
        tmp = FB_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(fb, f, ensure_ascii=False)
        os.replace(tmp, FB_PATH)

    dur = (datetime.now() - t0).total_seconds()
    log(f"run: writes={len(report['writes'])} conflicts={len(report['conflicts'])} "
        f"revised={len(report['revised'])} identity={len(report['identity'])} dry={DRY} dur={dur:.1f}s")
    for it in report['writes']:
        log(f"  + {it[0]} {it[1]} <- {it[2]}")
    for it in report['conflicts'][:10]:
        log(f"  ! conflict {it}")
    for it in report['revised']:
        log(f"  ~ revised {it}")

    if DRY:
        print(f"[DRY] writes={len(report['writes'])} conflicts={len(report['conflicts'])} "
              f"revised={len(report['revised'])} identity={len(report['identity'])} dur={dur:.1f}s")
        for it in report['writes'][:50]:
            print(f"  + {it[0]} {it[1]} <- {it[2]}")
        for it in report['conflicts'][:5]:
            print(f"  ! {it}")
        for it in report['identity'][:5]:
            print(f"  ~ identity {it}")
        return 0

    # ---- 告警(stdout→cron投递) ----
    target = (today + timedelta(days=1)).isoformat()
    tgt_day = fb.get(target, {})
    tgt_missing = [f for f, _, _, _ in FC_FIELDS if not complete_list(tgt_day.get(f))]
    if not complete_list(tgt_day.get('gen')) and 'gen' not in tgt_missing:
        tgt_missing.append('gen')
    if tgt_missing:
        print(f"⚠️ 融合预取: 目标日 {target} 预测未就绪/缺 {tgt_missing} (09:24主链前检查; 主链将按 系统→交易→zip 兜底)")
    if report['conflicts']:
        print(f"⚠️ 融合预取: A类不一致 {len(report['conflicts'])} 处, 例: {report['conflicts'][:2]}")
    if report['identity']:
        print(f"⚠️ 融合预取: 发电恒等式偏差 {report['identity'][:3]}")
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        print(f"❌ 融合预取异常: {e}")
        traceback.print_exc()
        sys.exit(1)
