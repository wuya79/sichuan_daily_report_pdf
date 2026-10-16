#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""水情缓存预热/追赶刷新 — 省平台重试式全量取真正最新记录 → 合并写回 .reservoir_cache.json

背景（2026-10-08 断供事故）：
- 备用接口 47.109.196.221:8088 挂掉后，日报 09:30 热窗口省平台成功率低 →
  get_all_reservoirs_latest() 内部把 48 站全部用旧缓存补满，日志仍显示"实时"，
  水位块连续多日字节级冻结且无任何告警。
- 另：生产 fetch_one 用 PAGESIZE=3 只读到窗口起点（昨天02:00），天生滞后 ~31h。

本脚本职责（cron 07:30 低峰运行 + 手动追赶共用）：
1) 全量拉取 48 站，每站最多 4 次尝试（平台单次成功率≈50%，双峰分布 0.1~0.2s / 10s+ 超时）；
2) 窗口 [D-4, D]，PAGESIZE=500（即使15分钟采样也不截尾），取 max(sj) 的真正最新记录；
3) 与现有缓存逐站合并：只在新记录时间戳 > 缓存时间戳时更新（绝不倒退），失败站保留原值；
4) 先 .bak_时间戳 备份，再原子写回缓存；原始记录落 /tmp 侧文件供对账/历史回填复用；
5) 默认即 cron 模式（no_agent 直投）：健康时零输出；降级时输出告警行。

用法:
  python3 sichuan_reservoir_preheat.py                  # cron 直跑：健康时零输出
  python3 sichuan_reservoir_preheat.py --verbose        # 手动查看：完整输出+明细
  python3 sichuan_reservoir_preheat.py --dry-run        # 只取数不写（带摘要输出）
  python3 sichuan_reservoir_preheat.py --probe 3        # 只测前3站（不写）
退出码: 0 正常（含部分站失败，用输出行告警）；1 灾难（缓存不可读写/写失败）
"""
import argparse
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

PROJ = '/home/ubuntu/sichuan_hydro_price'
CACHE = os.path.join(PROJ, '.reservoir_cache.json')
sys.path.insert(0, PROJ)

import sichuan_hydro_api as sh  # noqa: E402

ATTEMPTS = 4          # 单站最多尝试次数（双峰平台：~50%单次成功）
WORKERS = 8
WINDOW_DAYS = 4       # 取数窗口 [D-4, D]
PAGE_SIZE = 500       # 4天×15分钟粒度=384条，500 覆盖不截尾


def fetch_station(entry):
    """拉单站窗口记录。返回 (display_name, records|None, err)"""
    display, search = entry[0], entry[1]
    start = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()
    end = date.today().isoformat()
    last_err = 'no-attempt'
    for _att in range(1, ATTEMPTS + 1):
        try:
            recs = sh.get_reservoir_data(search, start_time=start, end_time=end,
                                         max_rows=PAGE_SIZE, timeout=(4, 5))
            if recs:
                return display, recs, ''
            last_err = 'empty'
        except Exception as e:
            last_err = type(e).__name__
        time.sleep(0.4)
    return display, None, last_err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--probe', type=int, default=0, help='只测前N站（不写库）')
    ap.add_argument('--verbose', action='store_true', help='完整输出+明细（默认：健康时零输出）')
    args = ap.parse_args()

    t0 = time.time()
    today = date.today()
    now_s = datetime.now().strftime('%m-%d %H:%M')

    # ── 读缓存（基线）──
    try:
        with open(CACHE, encoding='utf-8') as f:
            old = json.load(f)
        assert isinstance(old, list) and len(old) > 10
    except Exception as e:
        print(f'🔴 水情预热 {now_s}: 缓存读取失败 {type(e).__name__}: {e}')
        return 1
    old_by = {r.get('站名'): r for r in old}

    entries = list(sh.KEY_RESERVOIRS)
    if args.probe:
        entries = entries[:args.probe]

    # ── 全量取数（8线程 + 每站4次尝试）──
    results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_station, e): e for e in entries}
        for fut in as_completed(futs):
            d, recs, err = fut.result()
            results[d] = (recs, err)

    if args.probe:
        print(f'— probe {len(entries)}站 —')
        for e in entries:
            d = e[0]
            recs, err = results.get(d, (None, 'missing'))
            if recs:
                latest = max((r for r in recs if str(r.get('sj') or '').strip()),
                             key=lambda r: str(r.get('sj') or ''), default=None)
                sj = str(latest.get('sj') or '')[:16] if latest else '—'
                print(f'  {d}: {len(recs)}条, 最新 {sj}, ksw={latest.get("ksw") if latest else "?"}')
            else:
                print(f'  {d}: 失败({err})')
        return 0

    # ── 逐站合并（只进不退）──
    updated, unchanged, failed = [], [], []
    raw_dump = {}
    merged, covered = [], set()
    for entry in entries:
        display = entry[0]
        recs, err = results.get(display, (None, 'missing'))
        cur = old_by.get(display)
        new_rec = None
        if recs:
            cand = [r for r in recs if str(r.get('sj') or '').strip()]
            if cand:
                latest = max(cand, key=lambda r: str(r.get('sj') or ''))
                if latest.get('ksw') not in (None, '') or latest.get('xsl') not in (None, ''):
                    new_rec = latest
            raw_dump[display] = [
                {k: (str(r.get(k)) if r.get(k) is not None else None)
                 for k in ('sj', 'ksw', 'rkll', 'ckll', 'xsl')}
                for r in recs]

        if new_rec is None:
            if cur:
                merged.append(cur)
                covered.add(display)
            failed.append(f'{display}({err or "无有效记录"})')
            continue

        sj_new = str(new_rec.get('sj') or '')[:19]
        if not sj_new:
            if cur:
                merged.append(cur)
                covered.add(display)
            failed.append(f'{display}(无有效时间戳)')
            continue
        sj_old = str((cur or {}).get('时间', ''))
        if cur and sj_new <= sj_old:
            merged.append(cur)
            covered.add(display)
            unchanged.append(f'{display}(缓存{sj_old[:10]}更新)')
            continue

        rec_out = dict(cur or {})
        rec_out.update({
            '站名': display,
            '河流': entry[3],
            '流域': entry[4],
            '装机万kW': entry[2],
            '国调': entry[5],
            '库水位': str(new_rec.get('ksw', '') or '').strip(),
            '入库流量': str(new_rec.get('rkll', '') or '').strip(),
            '出库流量': str(new_rec.get('ckll', '') or '').strip(),
            '蓄水量': str(new_rec.get('xsl', '') or '').strip(),
            '时间': sj_new,
            '来源': 'main',
        })
        # 昨日出库：昨天日期内最后一条非空 ckll
        ymd = (today - timedelta(days=1)).isoformat()
        for r in reversed(recs or []):
            if str(r.get('sj', ''))[:10] == ymd and r.get('ckll') not in (None, '') \
                    and str(r.get('ckll')).strip():
                try:
                    rec_out['昨日出库'] = float(r.get('ckll'))
                except Exception:
                    pass
                break
        merged.append(rec_out)
        covered.add(display)
        updated.append(f'{display}({sj_new[5:16]})')

    # 保留未覆盖的旧站（防止遗漏）
    for r in old:
        if r.get('站名') not in covered:
            merged.append(r)

    # ── 新鲜度指标 ──
    ts_list = [r.get('时间', '')[:10] for r in merged if r.get('时间')]
    newest = max(ts_list) if ts_list else ''
    newest_age = (today - date.fromisoformat(newest)).days if newest else 999
    fresh2 = sum(1 for t in ts_list if (today - date.fromisoformat(t)).days <= 2)
    newest_full = max((r.get('时间', '') for r in merged if r.get('时间')), default='')

    # ── 写回（备份 + 原子）──
    if not args.dry_run:
        try:
            bak = CACHE + f'.bak_{datetime.now():%Y%m%d_%H%M}'
            shutil.copyfile(CACHE, bak)
            tmp = CACHE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(merged, f, ensure_ascii=False)
            os.replace(tmp, CACHE)
        except Exception as e:
            print(f'🔴 水情预热 {now_s}: 缓存写入失败 {type(e).__name__}: {e}')
            return 1

    # 原始记录侧文件（对账/历史回填复用）
    try:
        dump_path = f'/tmp/reservoir_preheat_raw_{today.strftime("%Y%m%d")}.json'
        with open(dump_path, 'w', encoding='utf-8') as f:
            json.dump({'generated_at': datetime.now().isoformat(), 'stations': raw_dump},
                      f, ensure_ascii=False)
    except Exception:
        dump_path = '(写入失败)'

    # ── 输出 ──
    degraded = []
    if updated and newest_age <= 1 and len(failed) <= 7:
        pass  # 健康
    else:
        if newest_age >= 3:
            degraded.append(f'🔴 全链最新仅 {newest}(D-{newest_age})，链已断供')
        elif newest_age == 2:
            degraded.append(f'⚠️ 全链最新 {newest}(D-2)，偏慢')
        if len(failed) >= 8:
            degraded.append(f'⚠️ {len(failed)}/48站取数失败')
        if not updated:
            degraded.append('⚠️ 0站更新（无任何新鲜数据）')

    ok_line = (f'✅ 水情预热 {now_s}: 更新{len(updated)}站 | 无变化{len(unchanged)} | '
               f'失败{len(failed)} | 全链最新 {newest_full}(D-{newest_age}) | '
               f'≤2天{fresh2}站 | 耗时{time.time()-t0:.0f}s')

    if args.verbose or args.dry_run:
        if degraded:
            print('｜'.join(degraded))
        print(ok_line)
        if args.verbose:
            if updated:
                print('  更新: ' + ', '.join(updated))
            if unchanged:
                print('  无变化: ' + ', '.join(unchanged))
            if failed:
                print('  失败: ' + ', '.join(failed))
        print(f'  侧档: {dump_path}' + ('  [dry-run 未写缓存]' if args.dry_run else f'  备份: {CACHE}.bak_*'))
    elif degraded:
        # cron 模式（零参数直跑）：健康时零输出；降级时输出一行告警
        # 2026-10-16：降级时顺带探测备用接口，把"两源是否全断"说清楚，免得每天猜
        _fb_note = ''
        if newest_age >= 2 or len(failed) >= 8:
            try:
                import requests as _rq
                _r = _rq.get(sh.FALLBACK_STATIONS_URL, timeout=3)
                if _r.status_code != 200 or not _r.json():
                    _fb_note = '；备用接口(47.109.196.221:8088)亦不通'
            except Exception:
                _fb_note = '；备用接口(47.109.196.221:8088)亦不通'
        print(f'⚠️ 水情预热 {now_s}: ' + '；'.join(degraded)
              + f'（更新{len(updated)}/无变化{len(unchanged)}/失败{len(failed)}）' + _fb_note)
    return 0


if __name__ == '__main__':
    sys.exit(main())
