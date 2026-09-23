#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数值与计算审计 — 售电/发电两日报关键数值独立重算（纯只读）

v1 (2026-09-23): 由来=当日两日报数值终审的53项固化为每日自动审计,
                 挂 audit_cron_guard.sh 第13项(每日16:00)。检查原理:
                 从原始数据源(快照/趋势库/价格库/月内档案/水情缓存/因子历史)
                 独立重算, 与报告"显示值"逐项比对。不核上游源数据自身正确性。

用法: python3 audit_daily_values.py [--date YYYY-MM-DD]     # 默认今天
退出码: 0=无❌(可含⚠️); 1=存在❌数值不一致或产物缺失
判定三级: ❌不一致(致命) / ⚠️无法核验(源缺/解析不到/已刷新, 仅列出) / ✅通过

说明:
- 报告解析失败→⚠️(可能格式变更), 不误报为数值错误
- 报告显式"未取得"的项自动跳过
- 水情类比对若缓存文件的mtime晚于报告mtime→⚠️跳过(数据已换批)
- 覆盖范围: 产物新鲜度/供需口径/⑥偏差表/趋势9行/价格与预测/月内交易/发电交叉
  (PDF内嵌值、上游源数据本身不在本层)
"""
import sys
sys.dont_write_bytecode = True  # 保持"零写IO"审计承诺

import argparse
import json
import re
from datetime import date as _date, datetime, timedelta
from pathlib import Path

SH = Path('/home/ubuntu/sichuan_hydro_price')
RP = Path('/var/www/reports')

RES = []  # (tag, level, disp, calc, note)


def s(tag, disp, calc, note=''):
    RES.append(('OK', tag, disp, calc, note))


def w(tag, msg, note=''):
    RES.append(('WARN', tag, msg, '', note))


def f(tag, disp, calc, note=''):
    RES.append(('FAIL', tag, disp, calc, note))


def sec(name):
    RES.append(('SEC', name, '', '', ''))


def _i(tok):
    """'39,550' -> 39550"""
    return int(str(tok).replace(',', '').replace(' ', ''))


def _load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _ratio_interval(a, fo):
    """显示整数±0.5 下可得偏差率区间(%) — 用于"显示四舍五入"数值的兼容判定"""
    lo = (a - 0.5 - (fo + 0.5)) / (fo + 0.5) * 100
    hi = (a + 0.5 - (fo - 0.5)) / (fo - 0.5) * 100
    return min(lo, hi), max(lo, hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None)
    args = ap.parse_args()
    D = args.date or _date.today().isoformat()
    dt = datetime.strptime(D, '%Y-%m-%d').date()
    Dm1 = (dt - timedelta(days=1)).isoformat()
    win_end = dt - timedelta(days=1)
    win_start = dt - timedelta(days=7)
    exp_hdr = (win_start.strftime('%m-%d'), win_end.strftime('%m-%d'))
    is_today = (dt == _date.today())

    sell_p = RP / f'daily_{dt.strftime("%Y%m%d")}.txt'
    gen_p = RP / 'gen_side_latest.txt'
    sell = sell_p.read_text(encoding='utf-8') if sell_p.exists() else None
    gen = gen_p.read_text(encoding='utf-8') if gen_p.exists() else None

    # ── 数据源(逐个安全加载) ──
    def safe(fn, tag):
        try:
            return fn()
        except Exception as e:
            w(tag, f'数据源不可用: {type(e).__name__} {e}')
            return None

    snap = safe(lambda: _load(SH / '.daily_fc_snapshot.json'), 'SRC fc快照')
    trends = safe(lambda: _load(SH / '.daily_trends.json'), 'SRC 趋势库')
    thermal = safe(lambda: _load(SH / '.thermal_trend.json'), 'SRC 火电库')
    ph = safe(lambda: _load(SH / '.price_history.json'), 'SRC 价格库')
    arch = safe(lambda: _load(SH / '.monthly_trade_archive.json'), 'SRC 月内档案')
    resv = safe(lambda: _load(SH / '.reservoir_cache.json'), 'SRC 水情缓存')
    rhist_meta = safe(lambda: _load(SH / '.reservoir_history.json')['_meta'], 'SRC 水库meta')

    try:
        sys.path.insert(0, str(SH))
        import factor_engine as fe
        import sichuan_hydro_api as shapi
        HAVE_FE = True
    except Exception as e:
        fe = shapi = None
        HAVE_FE = False
        w('SRC 因子引擎', f'导入失败: {type(e).__name__} {e}')

    # 水情缓存新鲜度闸门(缓存晚于报告=new批数据, 跳过比对)
    resv_fresh = True
    if resv is not None and sell_p.exists():
        try:
            if (SH / '.reservoir_cache.json').stat().st_mtime > sell_p.stat().st_mtime + 120:
                resv_fresh = False
        except Exception:
            pass

    def ph_rec(d):
        if not ph:
            return None
        return next((r for r in ph.get('records', []) if r.get('date') == d), None)

    def ph_valid(d):
        r = ph_rec(d)
        if not r:
            return None
        pts = r.get('price_96') or r.get('type_1_96') or []
        v = [(i, x) for i, x in enumerate(pts) if x is not None]
        return v or None

    def arch_latest():
        if not arch:
            return None
        ks = sorted(k for k in arch if re.match(r'^\d{4}-\d{2}-\d{2}$', k))
        return arch[ks[-1]] if ks else None

    gap_disp = None  # 供发电侧G9横比使用
    m74 = None       # 售电侧未解析时G9需可判空

    def roll_slots(target):
        a = arch_latest()
        if not a:
            return None
        return a.get('滚动', {}).get(target)

    def cont_slots():
        a = arch_latest()
        if not a:
            return None
        c = a.get('连续', {})
        return c.get('时段') if isinstance(c, dict) else None

    # ════════ A 产物 ════════
    sec('A 产物新鲜度')
    for tag, p, txt in [('A1 售电日报', sell_p, sell), ('A2 发电日报', gen_p, gen)]:
        if txt is None:
            f(tag, '文件不存在', str(p))
        else:
            mt = datetime.fromtimestamp(p.stat().st_mtime)
            if is_today and mt.date() != dt:
                f(tag, f'未当日更新(mtime {mt:%m-%d %H:%M})', '应为当日')
            else:
                s(tag, f'存在({mt:%m-%d %H:%M}, {len(txt)}B)', '新鲜')
    if not is_today:
        w('A0 历史日期模式', '数据源为当前态(非当日快照), 比对结果仅供参考')

    if sell:
        # ════════ B 供需口径 ════════
        sec('B 供需口径(售电)')
        m72 = re.search(r'负荷预测:\s*([\d,]+)MW \| 总可用:\s*([\d,]+)MW', sell)
        m73 = re.search(r'水电([\d,]+) \+ 新能源([\d,]+) \+ 非市场化([\d,]+)', sell)
        m74 = re.search(r'净负荷缺口:\s*(-?[\d,]+)MW', sell)
        m4s = re.search(r'净缺口(-?[\d,]+)MW\(过剩\)', sell)
        tot_disp = _i(m72.group(2)) if m72 else None
        parts = [_i(x) for x in m73.groups()] if m73 else None
        gap_disp = _i(m74.group(1)) if m74 else None
        # B1 分项和=总可用(显示自洽)
        if parts and tot_disp is not None:
            if sum(parts) == tot_disp:
                s('B1 总可用=分项取整和', f'{tot_disp:,}', f'{"+".join(map(lambda x: f"{x:,}", parts))}={sum(parts):,}')
            else:
                f('B1 总可用=分项取整和', f'{tot_disp:,}', f'分项和={sum(parts):,}', '分项与总数不符')
        else:
            w('B1 总可用', '解析不到(格式变更?)')
        # B1b 分项=源快照取整
        if parts and snap and D in snap:
            expect = [round(snap[D]['hydro']), round(snap[D]['re']), round(snap[D]['nim'])]
            if parts == expect:
                s('B1b 分项=源取整', f'{parts}', f'{expect}')
            else:
                f('B1b 分项=源取整', f'{parts}', f'{expect}')
        elif parts:
            w('B1b 分项源比对', 'fc快照缺当日')
        # B2 净缺口=浮点差
        if gap_disp is not None and snap and D in snap:
            gapf = snap[D]['load'] - (snap[D]['hydro'] + snap[D]['re'] + snap[D]['nim'])
            if round(gapf) == gap_disp:
                s('B2 净缺口=浮点差', f'{gap_disp:,}', f'{gapf:,.2f}→{round(gapf):,}')
            else:
                f('B2 净缺口=浮点差', f'{gap_disp:,}', f'{gapf:,.2f}→{round(gapf):,}')
        elif gap_disp is not None:
            w('B2 净缺口', 'fc快照缺当日')
        # B2b 负荷=源 + 三数±1MW自洽
        if m72 and snap and D in snap:
            ld = _i(m72.group(1))
            if round(snap[D]['load']) == ld:
                s('B2b 负荷=源取整', f'{ld:,}', f'{snap[D]["load"]:.2f}')
            else:
                f('B2b 负荷=源取整', f'{ld:,}', f'{round(snap[D]["load"]):,}')
        if tot_disp is not None and gap_disp is not None and m72:
            d1 = (_i(m72.group(1)) - tot_disp) - gap_disp
            if abs(d1) <= 1:
                s('B2c 三数自洽(±1MW)', f'负荷-总可用-缺口差={d1}', '取整路径差异, 允许±1')
            else:
                f('B2c 三数自洽(±1MW)', f'差={d1}', '偏差超过1MW')
        # B3 摘要净缺口=正文净缺口
        if m4s and gap_disp is not None:
            if _i(m4s.group(1)) == gap_disp:
                s('B3 摘要净缺口', f'{_i(m4s.group(1)):,}', f'正文{gap_disp:,}')
            else:
                f('B3 摘要净缺口', f'{_i(m4s.group(1)):,}', f'正文{gap_disp:,}')
        # B4 水电占比(水情行一位小数 + 摘要整数)
        mw_h = re.search(r'水电([\d,]+)MW\s*\(([\d.]+)%\)', sell)
        if mw_h and snap and D in snap:
            hp = snap[D]['hydro'] / snap[D]['load'] * 100
            ok_mw = (_i(mw_h.group(1)) == round(snap[D]['hydro']))
            ok_pct = abs(float(mw_h.group(2)) - hp) < 0.05
            s_abs = re.search(r'水电(\d+)%', sell)
            ok_abs = bool(s_abs and int(s_abs.group(1)) == round(hp))
            if ok_mw and ok_pct and ok_abs:
                s('B4 水电%', f'{mw_h.group(1)}MW/{mw_h.group(2)}%, 摘要{s_abs.group(1)}%', f'{hp:.2f}%→摘要{round(hp)}%')
            else:
                f('B4 水电%', f'mw={ok_mw} pct={ok_pct} 摘要={ok_abs}', f'重算{hp:.2f}%')
        else:
            w('B4 水电%', '水情行未解析到')
        # B5 周预测行
        m78 = re.search(r'周预测（D\+1-D\+4）：过剩([\d,]+)MW', sell)
        pairs = re.findall(r'(负荷|水电|光伏[（(]光照时段[）)]|风电)(\d+)→(\d+)([↑↓])(\d+)%', sell)
        if m78 and len(pairs) == 4:
            bad_ = []
            for lab, a, b, ar, pc in pairs:
                calc = abs(int(b) - int(a)) / int(a) * 100
                if abs(calc - int(pc)) > 1:
                    bad_.append(f'{lab}:显示{ar}{pc}% 算{calc:.1f}%')
            gapok = (abs(_i(m78.group(1))) == abs(gap_disp)) if gap_disp is not None else None
            if not bad_ and gapok:
                s('B5 周预测行', f'过剩{_i(m78.group(1)):,} + 4组%', '逐项复算一致(±1%容差)')
            else:
                f('B5 周预测行', f'{bad_ or ""} 过剩ok={gapok}', '存在不一致')
        else:
            w('B5 周预测行', '解析不到')
        # B6 火电负载率(两处一致 + 应=火电实际/开机)
        m5 = re.search(r'火电负载率([\d.]+)%', sell)
        m76 = re.findall(r'火电负载率([\d.]+)%', sell)
        m_hf = re.search(r'火电: 实际([\d,]+)MW', sell)
        if m5 and m_hf:
            rates = set(m76)
            hf = _i(m_hf.group(1))
            if thermal and Dm1 in thermal:
                thr = thermal[Dm1].get('running') or thermal[Dm1].get('running_capacity')
                if thr:
                    lo, hi = (hf - 0.5) / thr * 100, (hf + 0.5) / thr * 100
                    disp = float(m5.group(1))
                    okc = (lo - 0.05) <= disp <= (hi + 0.05)
                    if len(rates) == 1 and okc:
                        s('B6 火电负载率', f'{disp}%', f'{hf}/{thr:.0f}={hf/thr*100:.2f}%')
                    else:
                        f('B6 火电负载率', f'{disp}% 两处{rates}', f'{hf}/{thr:.0f}→{hf/thr*100:.2f}%')
                else:
                    w('B6 火电负载率', '火电库无开机容量')
            else:
                w('B6 火电负载率', '火电库缺昨日')
        else:
            w('B6 火电负载率', '解析不到')

        # ════════ C ⑥偏差表 ════════
        sec('C ⑥偏差表(售电)')
        c2disp = None
        for tag, lab in [('C1 负荷', '负荷'), ('C2 水电', '水电'), ('C3 光伏', '光伏'), ('C4 风电', '风电')]:
            m = re.search(re.escape(lab) + r': 实际([\d,]+)MW 预测([\d,]+) 偏差([+-][\d.]+)%', sell)
            if not m:
                w(tag, '解析不到')
                continue
            a_, f_, pc = _i(m.group(1)), _i(m.group(2)), float(m.group(3))
            lo, hi = _ratio_interval(a_, f_)
            if (lo - 0.05) <= pc <= (hi + 0.05):
                s(tag, f'{pc:+.1f}%', f'区间[{lo:+.2f},{hi:+.2f}]% (A={a_} F={f_})')
            else:
                f(tag, f'{pc:+.1f}%', f'区间[{lo:+.2f},{hi:+.2f}]% (A={a_} F={f_})')
            if lab == '水电':
                c2disp = pc
        m_c5 = re.search(r'水电偏差([+-]?\d+)%', sell)
        if m_c5 and c2disp is not None:
            if int(m_c5.group(1)) == round(c2disp):
                s('C5 告警行+43%', f'{m_c5.group(1)}%', f'round({c2disp})= {round(c2disp)}')
            else:
                f('C5 告警行', f'{m_c5.group(1)}%', f'round({c2disp})={round(c2disp)}')
        else:
            w('C5 告警行', '解析不到')

        # ════════ D 趋势仪表盘 ════════
        sec('D 趋势仪表盘(售电)')
        m_hdr = re.search(r'趋势仪表盘（(\d{2}-\d{2})~(\d{2}-\d{2}) 近7日）', sell)
        if m_hdr:
            if (m_hdr.group(1), m_hdr.group(2)) == exp_hdr:
                s('D0 窗口头', f'{m_hdr.group(1)}~{m_hdr.group(2)}', f'{exp_hdr[0]}~{exp_hdr[1]}')
            else:
                f('D0 窗口头', f'{m_hdr.group(1)}~{m_hdr.group(2)}', f'{exp_hdr[0]}~{exp_hdr[1]}')
        else:
            w('D0 窗口头', '解析不到')
        row_src = {
            '电价': ('avg_price', 'int'), '水电占比': ('hydro_ratio', 'int'),
            '来水指数': ('inflow_idx', '2f'), '负荷（预测）': ('load_avg', 'int'),
            '新能源（预测）': ('re_avg', 'int'), '德宝受入': ('debao_flow', 'int'),
            '滚动均价': ('trade_roll_price', 'int'), '省间净受入': ('net_import', 'int'),
            '火电开机': ('__thermal__', 'int'),
        }
        for lab, (key, fmt) in row_src.items():
            m = re.search(r'^  ' + re.escape(lab) + r': ([\d.]+(?:→[\d.]+)*) ([↑↓])(\d+)%', sell, re.M)
            if not m:
                w(f'D {lab}', '行未解析到')
                continue
            toks = m.group(1).split('→')
            arrow, pctd = m.group(2), int(m.group(3))
            src = None
            if key == '__thermal__' and thermal:
                ks = sorted(k for k in thermal if k <= Dm1)[-7:]
                vals = [thermal[k].get('running') for k in ks]
                if all(v is not None for v in vals) and len(vals) == 7:
                    src = [float(v) for v in vals]
            elif trends and key in trends:
                ks = sorted(k for k in trends[key] if k <= Dm1)[-7:]
                src = [trends[key][k] for k in ks]
            if not src or len(src) != 7 or len(toks) != 7:
                w(f'D {lab}', f'源窗口异常(toks={len(toks)})')
                continue
            vbad = []
            for tok, v in zip(toks, src):
                if fmt == '2f':
                    if abs(float(tok) - v) > 0.005:
                        vbad.append(f'{tok}≠{v:.2f}')
                else:
                    if int(round(v)) != int(float(tok)):
                        vbad.append(f'{tok}≠{round(v)}')
            pct = (src[-1] - src[0]) / abs(src[0]) * 100 if src[0] else None
            if pct is None:
                w(f'D {lab}', '首值=0无法算%')
                continue
            calc_arrow = '↑' if pct > 0 else '↓'
            calc_pct = f'{abs(pct):.0f}'
            if not vbad and calc_arrow == arrow and calc_pct == f'{pctd}':
                s(f'D {lab}', m.group(1) + f' {arrow}{pctd}%', f'{src[0]:.2f}→{src[-1]:.2f} {calc_arrow}{calc_pct}%')
            else:
                f(f'D {lab}', m.group(1) + f' {arrow}{pctd}%', f'{src[0]:.2f}→{src[-1]:.2f} {calc_arrow}{calc_pct}%',
                  ('值差异: ' + ','.join(vbad[:3])) if vbad else '变化%不符')

        # ════════ E 价格与预测 ════════
        sec('E 价格与预测(售电)')
        v = ph_valid(Dm1)
        avg22 = sum(x for _, x in v) / len(v) if v else None
        if v:
            mx = max(v, key=lambda t: t[1]); mn = min(v, key=lambda t: t[1])
        # E1 96点极值
        m131 = re.search(r'均价(\d+)元/MWh 高(\d+) 低(-?\d+)', sell)
        if m131 and v:
            da, dh, dl = int(m131.group(1)), int(m131.group(2)), int(m131.group(3))
            ca, ch, cl = round(avg22), round(mx[1]), round(mn[1])
            if (da, dh, dl) == (ca, ch, cl):
                s('E1 96点极值', f'均价{da} 高{dh} 低{dl}', f'{avg22:.2f} | {mx[1]:.2f} | {mn[1]:.2f}')
            else:
                f('E1 96点极值', f'均价{da} 高{dh} 低{dl}', f'{ca} | {ch} | {cl}')
        else:
            w('E1 96点极值', '解析不到或价格库缺昨日')
        # E2 昨日均价
        m3 = re.search(r'昨日均价(\d+)元', sell)
        if m3 and avg22 is not None:
            if int(m3.group(1)) == round(avg22):
                s('E2 昨日均价', f'{m3.group(1)}元', f'{avg22:.2f}')
            else:
                f('E2 昨日均价', f'{m3.group(1)}元', f'{round(avg22)}元')
        else:
            w('E2 昨日均价', '解析不到')
        # E3 相似日回测
        m82 = re.search(r'相似日回测: (\d+)日 \| 次日电价([↑↓])([\d.]+)% \| 准确率([\d.]+)% \(共(\d+)条历史\)', sell)
        if not HAVE_FE:
            w('E3 相似日回测', '因子引擎不可用')
        elif m82:
            try:
                fh = fe.load_history()
                rD = next((r for r in fh['records'] if r['date'] == D), None)
                if not rD:
                    w('E3 相似日回测', '因子历史缺当日')
                else:
                    bt = fe.get_similar_days_backtest(
                        D, current_score=rD['score'], current_hydro_ratio=rD.get('hydro_ratio'),
                        current_inflow=rD.get('inflow_idx'),
                        current_factors_full={k: rD.get(k) for k in
                            ['hydro_ratio', 'inflow_idx', 'thermal_util', 're_deviation', 'load_deviation', 'temp_anomaly']})
                    okk = (abs(bt['avg_price_change'] - float(m82.group(3))) < 0.05 and
                           abs(bt['win_rate'] - float(m82.group(4))) < 0.05 and
                           bt['history_count'] == int(m82.group(5)) and len(bt['similar_dates']) == int(m82.group(1)))
                    disp = f'{m82.group(1)}日 | {m82.group(2)}{m82.group(3)}% | {m82.group(4)}% | {m82.group(5)}条'
                    calc = f'{len(bt["similar_dates"])}日 | {bt["avg_price_change"]}% | {bt["win_rate"]}% | {bt["history_count"]}条'
                    (s if okk else f)('E3 相似日回测', disp, calc)
            except Exception as e:
                w('E3 相似日回测', f'重算异常: {type(e).__name__} {e}')
        else:
            w('E3 相似日回测', '解析不到')
        # E4 因子评分
        m163 = re.search(r'因子评分: (\S+)\(([+\-][\d.]+)\) 多(\d+)/空(\d+)', sell)
        if not HAVE_FE:
            w('E4 因子评分', '因子引擎不可用')
        elif m163:
            try:
                fh = fe.load_history()
                rD = next((r for r in fh['records'] if r['date'] == D), None)
                if not rD:
                    w('E4 因子评分', '因子历史缺当日')
                else:
                    fr = fe.compute_factors(
                        hydro_ratio=rD.get('hydro_ratio'), inflow_idx=rD.get('inflow_idx'),
                        thermal_utilization=rD.get('thermal_util'), re_deviation=rD.get('re_deviation'),
                        load_deviation=rD.get('load_deviation'), temp_anomaly=rD.get('temp_anomaly'), as_of=D)
                    dmap = {'bull': '偏多', 'bear': '偏空', 'neutral': '中性'}
                    sc = fr.get('score', 0)
                    calc = f'{dmap.get(fr.get("direction"), "中性")}({"+" if sc > 0 else ""}{sc}) 多{fr.get("bull_count", 0)}/空{fr.get("bear_count", 0)}'
                    full_calc = f'因子评分: {calc}'
                    if m163.group(0) == full_calc:
                        s('E4 因子评分', m163.group(0), calc)
                    else:
                        f('E4 因子评分', m163.group(0), full_calc)
            except Exception as e:
                w('E4 因子评分', f'重算异常: {type(e).__name__} {e}')
        else:
            w('E4 因子评分', '解析不到')
        # E5 KNN近3日
        m91 = re.search(r'近3日D\+2均价:\s*(\d+)元 \| 偏空比例\s*(\d+)%', sell)
        try:
            rows = [json.loads(l) for l in open(SH / 'hermes_generated/daily_samples.json', encoding='utf-8') if l.strip()]
            r3 = [x for x in rows if x.get('d2_avg_price') is not None][-3:]
        except Exception as e:
            r3 = None
        if m91 and r3 and len(r3) == 3:
            ka = sum(x['d2_avg_price'] for x in r3) / 3
            kb = sum(x.get('d2_bear_ratio') or 0 for x in r3) / 3 * 100
            if round(ka) == int(m91.group(1)) and round(kb) == int(m91.group(2)):
                s('E5 KNN近3日D+2', f'{m91.group(1)}元 | {m91.group(2)}%', f'{ka:.1f}元 | {kb:.0f}% (截至{r3[-1]["date"]})')
            else:
                f('E5 KNN近3日D+2', f'{m91.group(1)}元 | {m91.group(2)}%', f'{round(ka)}元 | {round(kb)}%')
        else:
            w('E5 KNN近3日', '解析不到或样本不足')
        # E6 prediction_log 交叉
        try:
            pls = [json.loads(l) for l in open(SH / '.prediction_log.jsonl', encoding='utf-8') if l.strip()]
            ent = [j for j in pls if j.get('d') == Dm1 and j.get('aa') is not None]
            if ent and avg22 is not None:
                aa = ent[-1]['aa']
                if abs(aa - avg22) < 0.01:
                    s('E6 prediction_log', f'{aa:.4f}', f'价格库均价{avg22:.4f}')
                else:
                    f('E6 prediction_log', f'{aa:.4f}', f'价格库均价{avg22:.4f}')
            else:
                w('E6 prediction_log', f'无{Dm1}的aa值(未填/待填)')
        except Exception as e:
            w('E6 prediction_log', f'读取异常: {type(e).__name__}')
        # E7 因子历史新鲜度
        if HAVE_FE:
            try:
                fh = fe.load_history()
                recs = {r['date']: r for r in fh['records']}
                miss = [d for d in (D, Dm1) if d not in recs or recs[d].get('score') is None]
                if miss:
                    w('E7 因子历史新鲜度', f'缺记录: {miss}')
                else:
                    s('E7 因子历史新鲜度', f'{D} & {Dm1} score在', 'OK')
            except Exception as e:
                w('E7 因子历史新鲜度', f'{type(e).__name__}')
        else:
            w('E7 因子历史新鲜度', '因子引擎不可用')

        # ════════ F 月内交易(售电) ════════
        sec('F 月内交易')
        for m in re.finditer(r'(\d+)/(\d+): 均价(\d+) \| 成交(\d+)MW \| 买卖比([\d.]+) \| 范围([\d-]+)', sell):
            mo, da, pavg, vol, ratio, rng = m.groups()
            target = f'{dt.year}-{int(mo):02d}-{int(da):02d}'
            slots = roll_slots(target)
            if slots is None:
                w(f'F {target[5:]}', '月内档案缺该目标')
                continue
            vol_c = sum(sl['成交量'] for sl in slots)
            buy = sum(sl['买量'] for sl in slots); sellq = sum(sl['卖量'] for sl in slots)
            wavg = sum(sl['均价'] * sl['成交量'] for sl in slots) / vol_c
            prices = [sl['均价'] for sl in slots]
            calc = f'{wavg:.1f} | {vol_c:.0f} | {buy/sellq:.2f} | {min(prices):.0f}-{max(prices):.0f}'
            disp = f'{pavg} | {vol} | {ratio} | {rng}'
            if round(wavg) == int(pavg) and round(vol_c) == int(vol) and f'{buy/sellq:.2f}' == ratio \
               and f'{min(prices):.0f}-{max(prices):.0f}' == rng:
                s(f'F {target[5:]} 月内', disp, calc)
            else:
                f(f'F {target[5:]} 月内', disp, calc)
        # F5s 连续交易(售电行)
        mcont = re.search(r'^\s+均价(\d+) \| 成交(\d+)MW \| 买卖比([\d.]+) \| 范围([\d-]+)', sell, re.M)
        cslots = cont_slots()
        if mcont and cslots:
            pairs = [(float(x['均价']), float(x['成交量'])) for x in cslots
                     if x.get('均价') and x.get('成交量')]
            wavg = sum(p * v for p, v in pairs) / sum(v for _, v in pairs)
            vol = sum(v for _, v in pairs)
            buy = sum(float(x['买量']) for x in cslots if x.get('买量'))
            sellq = sum(float(x['卖量']) for x in cslots if x.get('卖量'))
            prices = [float(x['均价']) for x in cslots if x.get('均价')]
            calc = f'{wavg:.2f} | {vol:.0f} | {buy/sellq:.2f} | {min(prices):.0f}-{max(prices):.0f}'
            disp = f'{mcont.group(1)} | {mcont.group(2)} | {mcont.group(3)} | {mcont.group(4)}'
            if round(wavg) == int(mcont.group(1)) and round(vol) == int(mcont.group(2)) \
               and f'{buy/sellq:.2f}' == mcont.group(3) and f'{min(prices):.0f}-{max(prices):.0f}' == mcont.group(4):
                s('F5s 连续交易(售电)', disp, calc)
            else:
                f('F5s 连续交易(售电)', disp, calc)
        elif mcont:
            w('F5s 连续交易', '月内档案缺连续')
        else:
            w('F5s 连续交易', '解析不到')

    if gen:
        # ════════ G 发电交叉 ════════
        sec('G 发电日报')
        # G1 出清偏差(重算 + 横比售电)
        mg1 = re.search(r'日前出清([\d,]+) MW vs 日内出清([\d,]+) MW，日内较日前([↑↓])(\d+)MW\(([+-][\d.]+)%\)', gen)
        if mg1:
            g_da, g_id = _i(mg1.group(1)), _i(mg1.group(2))
            diff = g_id - g_da
            pct = diff / g_da * 100
            calc_arrow = '↑' if pct > 0 else '↓'
            if calc_arrow == mg1.group(3) and abs(diff) == int(mg1.group(4)) and abs(pct - float(mg1.group(5))) < 0.05:
                s('G1 出清偏差', mg1.group(0)[-30:].strip(), f'{diff}MW({pct:+.1f}%)')
            else:
                f('G1 出清偏差', f'{mg1.group(3)}{mg1.group(4)}MW({mg1.group(5)}%)', f'{diff}MW({pct:+.1f}%)')
        else:
            w('G1 出清偏差', '解析不到')
        # G1b 横比售电出清偏差
        ms1 = re.search(r'日前([\d,]+)MW vs 日内([\d,]+)MW', sell) if sell else None
        if ms1 and mg1:
            if (int(ms1.group(1)), int(ms1.group(2))) == (_i(mg1.group(1)), _i(mg1.group(2))):
                s('G1b 出清横比', f'售电{ms1.group(1)}/{ms1.group(2)}', f'发电{mg1.group(1)}/{mg1.group(2)}')
            else:
                f('G1b 出清横比', f'售电{ms1.group(1)}/{ms1.group(2)}', f'发电{mg1.group(1)}/{mg1.group(2)}')
        # G2 综合来水指数
        mg2 = re.search(r'综合来水指数[:：]\s*(0\.\d+)', gen)
        if HAVE_FE and resv is not None and mg2 and resv_fresh:
            try:
                overall, basins_c, nn = shapi.compute_hydro_output_index(resv)
                if abs(float(mg2.group(1)) - overall) < 0.0005:
                    s('G2 综合来水指数', mg2.group(1), f'{overall} (n={nn})')
                else:
                    f('G2 综合来水指数', mg2.group(1), f'{overall}')
            except Exception as e:
                w('G2 综合来水指数', f'重算异常: {type(e).__name__} {e}')
        elif mg2 and not resv_fresh:
            w('G2 综合来水指数', '水情缓存晚于报告, 跳过')
        else:
            w('G2 综合来水指数', '解析不到或引擎不可用')
        # G3 流域行(重算 + 横比)
        mg3 = re.search(r'^\s*流域: (.+)$', gen, re.M)
        if HAVE_FE and resv is not None and mg3 and resv_fresh:
            try:
                _ov, basins_c, _nn = shapi.compute_hydro_output_index(resv)
                got = dict((nm, float(vv)) for nm, vv in re.findall(r'([\u4e00-\u9fa5]{2,4})(\d\.\d+)', mg3.group(1)))
                bads = [(nm, vv, basins_c.get(nm)) for nm, vv in got.items()
                        if nm in basins_c and abs(vv - basins_c[nm]) > 0.006]
                cross_ok = None
                if sell:
                    ms3 = re.search(r'^\s*流域: (.+)$', sell, re.M)
                    cross_ok = bool(ms3 and ms3.group(1).strip() == mg3.group(1).strip())
                if not bads and cross_ok is not False:
                    s('G3 流域行', mg3.group(1)[:38] + '…', '6流域复算一致' + ('' if cross_ok else ' (横比异常)'))
                else:
                    f('G3 流域行', str(bads[:2]), f'横比={cross_ok}')
            except Exception as e:
                w('G3 流域行', f'重算异常: {type(e).__name__} {e}')
        else:
            w('G3 流域行', '解析不到/缓存已刷新/引擎不可用')
        # G4 瀑布沟/紫坪铺 水位与蓄放水
        if resv is not None and rhist_meta and resv_fresh:
            try:
                cache_by = {x.get('站名') or x.get('name') or x.get('display_name'): x for x in resv}
                # 上一日缓存(取 .daily_trends.reservoir_daily? 用reservoir_history? 取cache中不具备 -> 用trends)
                prev = None
                if trends and 'reservoir_daily' in trends:
                    ks_ = sorted(k for k in trends['reservoir_daily'] if k <= Dm1)
                    if ks_:
                        prev = trends['reservoir_daily'][ks_[-1]]
            except Exception:
                cache_by, prev = {}, None
            # G4a 瀑布沟 水位+占比(2.1节行)
            mb = re.search(r'瀑布沟\([^)）]+\)\s*水位([\d.]+)[^\n]*?占调节库容(\d+)%', gen)
            bf = (cache_by or {}).get('瀑布沟')
            if mb and bf and '瀑布沟' in rhist_meta:
                lvl = float(bf.get('库水位')); cap = float(rhist_meta['瀑布沟']['reg_cap'])
                xsl = float(bf.get('蓄水量', 0))
                calc_lvl = f'{lvl:.1f}'; calc_cap = round(xsl / (cap * 100) * 100)
                if calc_lvl == mb.group(1) and str(calc_cap) == mb.group(2):
                    s('G4a 瀑布沟(水位/库容)', f'{mb.group(1)}m / {mb.group(2)}%', f'{calc_lvl}m / {calc_cap}%')
                else:
                    f('G4a 瀑布沟(水位/库容)', f'{mb.group(1)}m / {mb.group(2)}%', f'{calc_lvl}m / {calc_cap}%')
            else:
                w('G4a 瀑布沟', '解析不到或缓存缺站')
            # G4b 瀑布沟 蓄水行(delta)
            mbs = re.search(r'^→ 蓄水 \+([\d.]+)万m³ 占调节库容(\d+)% 水位([+-][\d.]+)m', gen, re.M)
            if mbs and bf and prev and '瀑布沟' in prev:
                pv = prev['瀑布沟']
                d_xsl = float(bf['蓄水量']) - float(pv['蓄水量'])
                d_lvl = float(bf['库水位']) - float(pv['库水位'])
                if abs(d_xsl - float(mbs.group(1))) < 0.005 and abs(d_lvl - float(mbs.group(3))) < 0.005:
                    s('G4b 瀑布沟蓄水行', f'+{mbs.group(1)}万m³ / {mbs.group(3)}m', f'{d_xsl:+.2f} / {d_lvl:+.2f}')
                else:
                    f('G4b 瀑布沟蓄水行', f'+{mbs.group(1)}万m³ / {mbs.group(3)}m', f'{d_xsl:+.2f} / {d_lvl:+.2f}')
            else:
                w('G4b 瀑布沟蓄水行', '解析不到或缺上日值')
            # G4c 紫坪铺
            mzp = re.search(r'紫坪铺\s+→ 蓄水 \+([\d.]+)万m³ 占调节库容(\d+)% 水位([+-][\d.]+)m', gen)
            zp = (cache_by or {}).get('紫坪铺')
            if mzp and zp and prev and '紫坪铺' in prev and '紫坪铺' in rhist_meta:
                pv = prev['紫坪铺']
                d_xsl = float(zp['蓄水量']) - float(pv['蓄水量'])
                d_lvl = float(zp['库水位']) - float(pv['库水位'])
                cap = float(rhist_meta['紫坪铺']['reg_cap'])
                calc_cap = round(float(zp['蓄水量']) / (cap * 100) * 100)
                okk = (abs(d_xsl - float(mzp.group(1))) < 0.005 and str(calc_cap) == mzp.group(2)
                       and abs(d_lvl - float(mzp.group(3))) < 0.005)
                calc = f'+{d_xsl:.2f}/{calc_cap}%/{d_lvl:+.2f}'
                disp = f'+{mzp.group(1)}/{mzp.group(2)}%/{mzp.group(3)}'
                (s if okk else f)('G4c 紫坪铺', disp, calc)
            else:
                w('G4c 紫坪铺', '解析不到或缺上日值')
        else:
            w('G4 水情块', '缓存已刷新或缺meta')
        # G5 火电利用率
        mg5 = re.search(r'火电利用率(\d+)%', gen)
        mg_hf = re.search(r'火电开机参考\s*([\d,]+) MW（\d+台）\| 停机 (\d+)台/([\d,]+) MW', gen)
        if mg5 and mg_hf:
            on, off = _i(mg_hf.group(1)), _i(mg_hf.group(3))
            util = round(on / (on + off) * 100)
            if util == int(mg5.group(1)):
                s('G5 火电利用率', f'{mg5.group(1)}%', f'{on}/({on}+{off})={on/(on+off)*100:.1f}%')
            else:
                f('G5 火电利用率', f'{mg5.group(1)}%', f'{util}%')
        else:
            w('G5 火电利用率', '解析不到')
        # G6 升水(现货/滚动/月度)
        v22 = ph_valid(Dm1)
        spot = sum(x for _, x in v22) / len(v22) if v22 else None
        roll_f = None
        if trends and 'trade_roll_price' in trends:
            roll_f = trends['trade_roll_price'].get(D)
        monthly_f = None
        try:
            mp = _load(SH / '.monthly_platform_prices.json')
            month_key = dt.strftime('%Y-%m')
            prices = [p.get('platform_price') for p in mp.get(month_key, {}).get('prices', {}).values() if p.get('platform_price')]
            monthly_f = sum(prices) / len(prices) if prices else None
        except Exception:
            pass
        mg6 = re.search(r'滚动D\+2~D\+4\s+(\d+) 元/MWh\s+升水([+-]\d+)元', gen)
        mg6m = re.search(r'月度交易价格\s+(\d+) 元/MWh\s+升水([+-]\d+)元', gen)
        mg6s = re.search(r'现货（昨日）\s+(\d+) 元/MWh', gen)
        items_ok, items_bad = [], []
        if mg6 and roll_f is not None and spot is not None:
            okk = (int(mg6.group(1)) == round(roll_f) and int(mg6.group(2)) == round(roll_f - spot))
            (items_ok if okk else items_bad).append(f'滚动{mg6.group(1)}/升水{mg6.group(2)}')
        if mg6m and monthly_f is not None and spot is not None:
            okk = (int(mg6m.group(1)) == round(monthly_f) and int(mg6m.group(2)) == round(monthly_f - spot))
            (items_ok if okk else items_bad).append(f'月度{mg6m.group(1)}/升水{mg6m.group(2)}')
        if mg6s and spot is not None:
            okk = int(mg6s.group(1)) == round(spot)
            (items_ok if okk else items_bad).append(f'现货{mg6s.group(1)}')
        if items_ok or items_bad:
            calc = f'现货{spot:.2f} 滚动{roll_f and f"{roll_f:.2f}"} 月度{monthly_f and f"{monthly_f:.2f}"}'
            if items_bad:
                f('G6 升水三项', f'坏:{items_bad}', calc)
            else:
                s('G6 升水三项', '现货20/滚动28+8/月度109+89', calc)
        else:
            w('G6 升水三项', '解析不到')
        # G7 月内D+2~4 横比售电
        g7 = re.findall(r'D\+([234])\((\d+)/(\d+)\)\s+(\d+)\s+([\d-]+)', gen)
        if g7 and sell:
            bads = []
            for _n, mo, da, pavg, rng in g7:
                ms = re.search(rf'{int(mo)}/{int(da)}: 均价(\d+) \| 成交(\d+)MW \| 买卖比[\d.]+ \| 范围([\d-]+)', sell)
                if not ms or ms.group(1) != pavg or ms.group(3) != rng:
                    bads.append(f'{mo}/{da}: 发电{pavg}/{rng} 售电{ms.groups() if ms else None}')
            if not bads:
                s('G7 月内横比', f'{len(g7)}个目标一致', '发电D+2~4=售电9/x')
            else:
                f('G7 月内横比', str(bads[:2]), '')
        else:
            w('G7 月内横比', '解析不到')
        # G8 火电开机参考
        mg8 = re.search(r'火电开机参考\s*([\d,]+) MW', gen)
        thr = None
        if thermal:
            ks_ = sorted(k for k in thermal if k <= D)
            if ks_:
                thr = thermal[ks_[-1]].get('running')
        if mg8 and thr is not None:
            if _i(mg8.group(1)) == round(thr):
                s('G8 火电开机', f'{mg8.group(1)}MW', f'火电库{thr:.0f}MW')
            else:
                f('G8 火电开机', f'{mg8.group(1)}MW', f'{round(thr)}MW')
        else:
            w('G8 火电开机', '解析不到')
        # G9 净缺口横比
        mg9 = re.search(r'净缺口\s*(-?\d+)MW', gen)
        if mg9 and m74:
            if int(mg9.group(1)) == gap_disp:
                s('G9 净缺口横比', f'{mg9.group(1)}MW', f'售电{0 if gap_disp is None else gap_disp}MW')
            else:
                f('G9 净缺口横比', f'{mg9.group(1)}MW', f'售电{gap_disp}MW')
        elif mg9:
            w('G9 净缺口横比', '售电侧未解析')
        else:
            w('G9 净缺口横比', '解析不到')
        # G10 水电占比横比
        mg10 = re.search(r'水电占比(\d+)%', gen)
        ms10 = re.search(r'水电(\d+)%', sell) if sell else None
        if mg10 and ms10:
            if mg10.group(1) == ms10.group(1):
                s('G10 水电占比横比', f'{mg10.group(1)}%', f'售电{ms10.group(1)}%')
            else:
                f('G10 水电占比横比', f'{mg10.group(1)}%', f'售电{ms10.group(1)}%')
        else:
            w('G10 水电占比横比', '解析不到')
        # G11 火电开机近7日趋势行
        mg11 = re.search(r'趋势：近7日(\d+)→(\d+)MW', gen)
        if mg11 and thermal:
            ks_ = sorted(k for k in thermal if k <= Dm1)[-7:]
            if len(ks_) == 7:
                a_, b_ = thermal[ks_[0]].get('running'), thermal[ks_[-1]].get('running')
                if int(mg11.group(1)) == round(a_) and int(mg11.group(2)) == round(b_):
                    s('G11 开机近7日', f'{mg11.group(1)}→{mg11.group(2)}MW', f'{a_:.0f}→{b_:.0f}')
                else:
                    f('G11 开机近7日', f'{mg11.group(1)}→{mg11.group(2)}', f'{a_:.0f}→{b_:.0f}')
            else:
                w('G11 开机近7日', '火电库窗口不足')
        else:
            w('G11 开机近7日', '解析不到')
        # G12 六板块7行横比
        gpairs = [('电价', '电价'), ('水电占比', '水电占比'), ('来水指数', '来水指数'),
                  ('负荷', '负荷（预测）'), ('新能源', '新能源（预测）'), ('滚动均价', '滚动均价'), ('火电开机', '火电开机')]
        bads = []
        for gl, sl in gpairs:
            mg = re.search(r'^  ' + re.escape(gl) + r': ([\d.]+(?:→[\d.]+)*) ([↑↓])(\d+)%', gen, re.M)
            ms = re.search(r'^  ' + re.escape(sl) + r': ([\d.]+(?:→[\d.]+)*) ([↑↓])(\d+)%', sell, re.M) if sell else None
            if not mg or not ms or mg.groups() != ms.groups():
                bads.append(gl)
        if not bads and gpairs:
            s('G12 六板块横比', '7行与售电一致', '')
        elif bads:
            f('G12 六板块横比', f'不一致: {bads}', '')
        else:
            w('G12 六板块横比', '解析不到')
        # G13 gen趋势窗口头
        mg13 = re.search(r'趋势仪表盘（近7日 (\d{2}-\d{2})~(\d{2}-\d{2})）', gen)
        if mg13:
            if (mg13.group(1), mg13.group(2)) == exp_hdr:
                s('G13 发电窗口头', f'{mg13.group(1)}~{mg13.group(2)}', f'{exp_hdr[0]}~{exp_hdr[1]}')
            else:
                f('G13 发电窗口头', f'{mg13.group(1)}~{mg13.group(2)}', f'{exp_hdr[0]}~{exp_hdr[1]}')
        else:
            w('G13 发电窗口头', '解析不到')
        # G14 发电连续交易行
        mg14 = re.search(r'^(\d+)/(\d+)\s+(\d+)\s+-\s*$', gen, re.M)
        cslots2 = cont_slots()
        if mg14 and cslots2:
            pairs = [(float(x['均价']), float(x['成交量'])) for x in cslots2 if x.get('均价') and x.get('成交量')]
            wavg = sum(p * v for p, v in pairs) / sum(v for _, v in pairs)
            if round(wavg) == int(mg14.group(3)):
                s('G14 发电连续行', f'{mg14.group(1)}/{mg14.group(2)} 均价{mg14.group(3)}', f'档案加权{wavg:.2f}')
            else:
                f('G14 发电连续行', f'{mg14.group(3)}', f'{round(wavg)}')
        else:
            w('G14 发电连续行', '解析不到')
        # G15 滚动加权均价
        mg15 = re.search(r'滚动加权均价：(\d+)', gen)
        if mg15 and g7:
            allpv = []
            for _n, mo, da, _p, _r in g7:
                slots = roll_slots(f'{dt.year}-{int(mo):02d}-{int(da):02d}')
                if slots:
                    allpv += [(sl['均价'], sl['成交量']) for sl in slots]
            if allpv:
                wavg = sum(p * v for p, v in allpv) / sum(v for _, v in allpv)
                if round(wavg) == int(mg15.group(1)):
                    s('G15 滚动加权均价', f'{mg15.group(1)}', f'档案{wavg:.2f}')
                else:
                    f('G15 滚动加权均价', f'{mg15.group(1)}', f'{round(wavg)}')
        else:
            w('G15 滚动加权均价', '解析不到')

    # ════════ 输出 ════════
    print(f'数值与计算审计 {D}（独立重算，只读）')
    print('数据源: fc快照/趋势库/火电库/价格库/月内档案/水情缓存/因子历史')
    n_ok = n_w = n_f = 0
    n_item = 0
    for lv, tag, disp, calc, note in RES:
        if lv == 'SEC':
            print(f'\n[{tag}]')
            continue
        n_item += 1
        mark = {'OK': '✅', 'WARN': '⚠️', 'FAIL': '❌'}[lv]
        if lv == 'OK':
            n_ok += 1
        elif lv == 'WARN':
            n_w += 1
        else:
            n_f += 1
        print(f'{mark} {tag:22s} {disp}')
        if calc:
            print(f'      ↳ 重算: {calc}')
        if note:
            print(f'      ↳ {note}')
    warns = [t for lv, t, *_ in RES if lv == 'WARN']
    fails = [t for lv, t, *_ in RES if lv == 'FAIL']
    print()
    if warns:
        print('⚠️清单: ' + '; '.join(warns))
    if fails:
        print('❌清单: ' + '; '.join(fails))
    print(f'共 {n_item} 项 | ✅{n_ok} ⚠️{n_w} ❌{n_f}')
    return 1 if n_f else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'❌ 审计脚本异常: {type(e).__name__}: {e}')
        print('共 0 项 | ✅0 ⚠️0 ❌1')
        sys.exit(1)
