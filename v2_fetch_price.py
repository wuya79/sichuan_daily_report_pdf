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
TRADE_URL = 'http://127.0.0.1:45678/api/trade'
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
        bad = [(h, round(v, 1)) for h, v in enumerate(arr) if not (0 <= v <= 1500)]
        if bad:
            # 2026-09-12守卫: 重庆限价0-1500；越限值=官方行退化特征(负值28格实证)，拒收待人工核实
            print(f"⚠ {rec.get('date')} {key} 存在越限值[0,1500]: {bad[:6]} → 拒绝写入, 请人工核实")
            return False
    return True


def _fetch_rt_trade(ds):
    """系统 t2 空时，用交易接口 t12（实时出清结果）兜底 rt。
    口径：实时出清结果（正常日与节点价基本一致、个别点有差——见 skill 验证文档）。
    只读；密钥由 cq-web 注入；仅在系统通道当日空时触发。
    v1.2(2026-09-10): 逐点容错——单个坏点跳过不拖垮整次兜底；千分位逗号兼容。"""
    try:
        req = urllib.request.Request(
            TRADE_URL,
            json.dumps({"data_type": 12, "info_date": ds}).encode(),
            {"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        out, skipped = {}, 0
        for x in r.get("data", []) or []:
            try:
                if str(x.get("info_type")) != "1":
                    continue
                hh, mm = str(x.get("info_time")).split(':')
                out[f"{int(hh):02d}{int(mm):02d}"] = float(str(x.get("info_value")).replace(",", ""))
            except Exception:
                skipped += 1
        if skipped:
            print(f"  ⚠ {ds}: 交易t12兜底解析跳过 {skipped} 个异常点（其余 {len(out)} 点已采用）")
        return out
    except Exception as e:
        print(f"  ⚠ {ds}: 交易t12兜底调用异常: {e}")
        return {}


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
    if not rt_raw:
        _rt_fb = _fetch_rt_trade(ds)
        if _rt_fb:
            rt_raw = _rt_fb
            print(f"  ℹ {ds}: 系统 rt 空 → 交易t12（实时出清结果）兜底 {len(_rt_fb)}点")
        else:
            print(f"  ⚠ {ds}: rt 系统通道空，交易t12兜底亦不可用")
    da_list = _aggregate_to_hourly(da_raw)
    rt_list = _aggregate_to_hourly(rt_raw)
    if not da_raw:
        # 2026-09-02守卫; v1.2(2026-09-10): 改为 da 空即提示——rt 空现由兜底专线日志覆盖，da 空无论 rt 状态都须可见
        print(f"⚠ {ds}: da 原始点为空（t5未发布或字段名变化），请检查API返回结构；rt侧{'有' if rt_raw else '无'}点")
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

    # v1.3(2026-09-10): 最近7天缺口扫描——中间缺失日补拉（覆盖"非连续段"缺口）
    _recent = [(date.fromisoformat(yesterday) - timedelta(days=k)).isoformat() for k in range(7)]
    _missing = [ds for ds in _recent if ds not in by_date]
    if _missing:
        targets = sorted(set(targets) | set(_missing))

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
