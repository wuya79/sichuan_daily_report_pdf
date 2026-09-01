#!/usr/bin/env python3
"""cron脚本治理审计 v2 — 纯只读,零写入
v1(2026-08-20): 8项基础检查(commit/引用/镜像/语法/敏感/文件/cron/working tree)
v2(2026-08-20): +退出码汇总(可CI) +镜像权限一致性 +.gitignore例外实测
                +文档漂移检查(OPERATIONS vs jobs.json) +管道吞错反模式扫描
用法: python3 audit_cron_changes.py [--since YYYY-MM-DD]
退出码: 0=全部OK; 1=存在FAIL/DIFF/MISS/ALERT/HIT(可挂cron自动告警)
"""
import subprocess
import sys
import ast
from pathlib import Path

HOME = Path.home()
V2 = HOME / 'v2_cq_strategy'
SH = HOME / 'sichuan_hydro_price'
NEWS = HOME / 'sichuan_news_brief'
GS = HOME / 'sichuan_gen_side_report'
SC = HOME / '.hermes/scripts'

SINCE = None
argv = sys.argv[1:]
if '--since' in argv:
    i = argv.index('--since')
    if i + 1 < len(argv):
        SINCE = argv[i + 1]
if SINCE is None:
    from datetime import datetime
    SINCE = datetime.now().strftime('%Y-%m-%d')

FAILS = []  # (section, item) 所有失败项,决定退出码

def sh(cmd, cwd=None, timeout=90):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return r

def sec(t):
    print()
    print('=' * 60)
    print(t)
    print('=' * 60)

def fail(section, item):
    FAILS.append((section, item))

sec(f'1. commit清单(since={SINCE})')
for name, path in [('v2_cq_strategy', V2), ('sichuan_hydro_price', SH),
                   ('sichuan_news_brief', NEWS), ('sichuan_gen_side_report', GS)]:
    r = sh(f'git log --oneline --since="{SINCE} 00:00" | cat', cwd=path)
    print(f'[{name}] {len(r.stdout.strip().splitlines())} commits')
    for line in (r.stdout or '').strip().splitlines():
        print('  ', line)

sec('2. 被删资源全盘引用扫描')
scan = [str(HOME/d) for d in ['v2_cq_strategy', 'sichuan_hydro_price', 'sichuan_news_brief',
                              'sichuan_weather_brief', 'sichuan_weekly_report',
                              'sichuan_gen_side_report', '.hermes/scripts', '.hermes/cron']]
deleted = {
    'gen_side目录引用': 'hermes/scripts/gen_side',
    '_25d模型文件引用': 'xgb_cls_direction_25d|xgb_reg_spread_25d|model_registry_25d',
}
NOTE_FILES = ('SKILL.md', '开发日志.md', 'OPERATIONS.md', '操作手册.md')
for label, pat in deleted.items():
    hits = []
    for d in scan:
        r = sh(f"grep -rnE '{pat}' {d} --include='*.py' --include='*.sh' --include='*.js' --include='*.json' 2>/dev/null | grep -v '/\\.git/' | grep -v '/backup/'")
        for line in (r.stdout or '').splitlines():
            # 排除: 治理说明文档 / 审计脚本自身 / 含"已删除"的说明性注释
            if any(nf in line for nf in NOTE_FILES):
                continue
            if 'audit_cron_changes.py' in line:
                continue
            if '已删除' in line:
                continue
            hits.append(line)
    if hits:
        fail(2, label)
    print(f'{label}: {"CLEAN" if not hits else "FOUND " + str(len(hits))}')
    for h in hits[:8]:
        print('   ', h[:160])

sec('3. 镜像一致性+权限位(diff -q + 可执行位)')
pairs = [
    (SC/'v2_daily.sh', V2/'cron/v2_daily.sh'),
    (SC/'v2_append_daily.py', V2/'cron/v2_append_daily.py'),
    (SC/'v2_retrain.sh', V2/'cron/v2_retrain.sh'),
    (SC/'v2_retrain_25d.sh', V2/'cron/v2_retrain_25d.sh'),
    (SC/'v2_fetch_price.py', V2/'cron/v2_fetch_price.py'),
    (SC/'v2_single_stage.py', V2/'cron/v2_single_stage.py'),
    (SC/'v2_health_report.sh', V2/'cron/v2_health_report.sh'),
    (SC/'morning_report_watchdog.py', V2/'cron/morning_report_watchdog.py'),
    (SC/'v2_data_backup.py', V2/'cron/v2_data_backup.py'),
    (SC/'v2_push_csv_wecom.py', V2/'cron/v2_push_csv_wecom.py'),
    (SC/'v2_backtest_summary.py', V2/'cron/v2_backtest_summary.py'),
    (SC/'audit_cron_changes.py', V2/'cron/audit_cron_changes.py'),
    (SC/'audit_cron_guard.sh', V2/'cron/audit_cron_guard.sh'),
    (SC/'sichuan_daily_report.sh', SH/'scripts/sichuan_daily_report.sh'),
    (SC/'pdf_report_generator.py', SH/'scripts/pdf_report_generator.py'),
    (SC/'key_loader.py', SH/'scripts/key_loader.py'),
]
for src, dst in pairs:
    if not src.exists() or not dst.exists():
        print(f'MISSING {dst.name}')
        fail(3, dst.name)
        continue
    r = sh(f'diff -q "{src}" "{dst}"')
    content_ok = r.returncode == 0
    exec_src = src.stat().st_mode & 0o111
    exec_dst = dst.stat().st_mode & 0o111
    perm_ok = bool(exec_src) == bool(exec_dst)
    status = 'OK  ' if (content_ok and perm_ok) else ('DIFF' if not content_ok else 'PERM')
    if not (content_ok and perm_ok):
        fail(3, dst.name)
    print(f'{status} {dst.name}')

sec('4. .gitignore例外实测(git check-ignore)')
# 3个生产脚本必须不被忽略; 一次性脚本必须被忽略
must_track = [SH/'scripts/sichuan_daily_report.sh', SH/'scripts/pdf_report_generator.py', SH/'scripts/key_loader.py']
must_ignore = [SH/'scripts/some_one_off_analysis.py', SH/'scripts/cq_monthly/new_file.py']
for f in must_track:
    r = sh(f'git check-ignore "{f}"', cwd=SH)
    if r.returncode == 0:
        fail(4, f.name)
    print(('OK  ' if r.returncode != 0 else 'FAIL') + f' 应跟踪: {f.name}')
for f in must_ignore:
    r = sh(f'git check-ignore "{f}"', cwd=SH)
    if r.returncode != 0:
        fail(4, f.name)
    print(('OK  ' if r.returncode == 0 else 'FAIL') + f' 应忽略: {f.name}')

sec('5. 语法/编译检查')
shs = ['v2_daily.sh', 'v2_retrain.sh', 'v2_retrain_25d.sh', 'v2_health_report.sh', 'sichuan_daily_report.sh']
for f in shs:
    r = sh(f'bash -n "{SC/f}"')
    if r.returncode != 0:
        fail(5, f)
    print(('OK  ' if r.returncode == 0 else 'FAIL') + f' bash -n {f}')
pys = [SC/'api_push.py', SC/'key_loader.py', SC/'pdf_report_generator.py',
       SC/'v2_fetch_price.py', SC/'v2_append_daily.py', SC/'v2_data_backup.py',
       SC/'morning_report_watchdog.py', V2/'v2_daily.py', V2/'retrain.py',
       V2/'v2_health_report.py', NEWS/'fetch_morning.py', NEWS/'fetch_afternoon.py',
       NEWS/'lib/formatter.py', GS/'gen_txt.py', GS/'gen_side_ds_pdf.py',
       SH/'scripts/cq_monthly/daily_signal.py']
for f in pys:
    if not f.exists():
        continue
    # ast.parse纯语法解析——零写IO(2026-08-20: py_compile会写__pycache__/.pyc,违背"只读审计"承诺)
    try:
        with open(f, encoding='utf-8') as fh:
            ast.parse(fh.read())
        ok = True
    except SyntaxError:
        ok = False
    if not ok:
        fail(5, f.name)
    print(('OK  ' if ok else 'FAIL') + f' ast {f.name}')

sec('6. 失败模式扫描(管道吞错反模式: | tail -N 丢弃上游退出码)')
bad_pipes = []
for f in SC.glob('v2_*.sh'):
    for i, line in enumerate(f.read_text().splitlines(), 1):
        # 仅标记"进程|tail"吞错模式; 跳过注释行与echo变量|tail(无害)
        s = line.strip()
        if s.startswith('#'):
            continue
        if '| tail -' in line and not line.lstrip().startswith('echo'):
            bad_pipes.append((f.name, i, line.strip()))
for f in SC.glob('*.sh'):
    if 'v2_' in f.name:
        continue
    for i, line in enumerate(f.read_text().splitlines(), 1):
        s = line.strip()
        if s.startswith('#'):
            continue
        if '| tail -' in line and not line.lstrip().startswith('echo'):
            bad_pipes.append((f.name, i, line.strip()))
for name, ln, content in bad_pipes:
    fail(6, f'{name}:{ln}')
    print(f'BAD  {name}:{ln} {content[:90]}')
if not bad_pipes:
    print('OK   所有sh无 | tail -N 吞错管道')

sec('7. 入库文件敏感信息扫描')
new_files = [V2/'cron', V2/'scripts/api_push.py', SH/'scripts/key_loader.py',
             SH/'scripts/pdf_report_generator.py', SH/'scripts/sichuan_daily_report.sh']
pat = r'sk-[A-Za-z0-9]{20,}|api[_-]?key\s*[:=]\s*["\x27][A-Za-z0-9]{16,}|token\s*[:=]\s*["\x27][A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|PRIVATE)'
for f in new_files:
    r = sh(f"grep -rnE '{pat}' '{f}' 2>/dev/null")
    name = f.name if f.is_file() else 'cron/'
    if r.returncode == 0:
        fail(7, name)
        print('HIT ' + name)
        for line in r.stdout.splitlines()[:5]:
            print('   ', line[:120])
    elif r.returncode == 1:
        print('OK  ' + name)
    else:
        fail(7, name)
        print('ERR ' + name + ' (grep执行失败)')

sec('8. V2运行时关键文件存在性')
need = ['xgb_reg_spread.json', 'xgb_cls_direction.json', 'xgb_cls_direction_2.json',
        'xgb_cls_direction_3.json', 'xgb_reg_spread_35d.json', 'xgb_cls_direction_35d.json',
        'xgb_cls_direction_2_35d.json', 'xgb_cls_direction_3_35d.json',
        'xgb_mag_spread.json', 'xgb_mag_spread_2.json', 'xgb_mag_spread_3.json',
        'model_registry.json', 'model_registry_35d.json', 'structural_fit.json',
        'data/features_enhanced.csv', 'output/cq_price_history.json']
for f in need:
    p = V2 / 'models' / f if '/' not in f else V2 / f
    if not p.exists():
        fail(8, f)
    print(('OK  ' if p.exists() else 'MISS') + f' {f}')

sec('9. cron jobs.json完整性')
import json
d = json.load(open(HOME / '.hermes/cron/jobs.json'))
for j in d.get('jobs', []):
    s = j.get('script')
    if s:
        p = SC / s
        jid = (j.get('id') or j.get('job_id') or '?')[:12]
        if not p.exists():
            fail(9, s)
        print(('OK  ' if p.exists() else 'MISS') + f' [{jid}] {s}')
    elif j.get('no_agent'):
        print(f'WARN no_agent无script: {j.get("name")}')

sec('10. 文档漂移(OPERATIONS.md cron表 vs jobs.json实际schedule)')
op_path = V2 / 'OPERATIONS.md'
op_text = op_path.read_text() if op_path.exists() else ''
import re
# OPERATIONS.md cron表行: | 每日09:26 | ... | `v2_daily.sh` ... |
doc_entries = re.findall(r'\|\s*每日(\d{2}):(\d{2})\s*\|[^|]*\|\s*`([a-zA-Z0-9_.]+)`', op_text)
job_sched = {j.get('script'): j.get('schedule') for j in d.get('jobs', []) if j.get('script')}
drift = 0
for hh, mm, script in doc_entries:
    if script not in job_sched:
        continue
    actual = job_sched[script]
    # jobs.json中schedule是dict {kind, expr, display}; 兼容字符串
    expr = actual.get('expr') if isinstance(actual, dict) else actual
    if not expr:
        continue
    parts = expr.split()
    if len(parts) >= 2 and (int(parts[1]) != int(hh) or int(parts[0]) != int(mm)):
        drift += 1
        fail(10, script)
        print(f'DIFF {script}: 文档{hh}:{mm} vs 实际cron {parts[1]}:{parts[0]} ({actual})')
    else:
        print(f'OK   {script}: 文档{hh}:{mm} = cron实际')
if drift == 0:
    print(f'OK   文档cron表与jobs.json无时间漂移(共核对{len(doc_entries)}条)')

sec('11. working tree(代码dirty断言)')
CODE_SUFFIX = ('.py', '.sh', '.js')
for name, path in [('v2_cq_strategy', V2), ('sichuan_hydro_price', SH),
                   ('sichuan_news_brief', NEWS), ('sichuan_gen_side_report', GS),
                   ('sichuan_weather_brief', HOME/'sichuan_weather_brief'),
                   ('sichuan_weekly_report', HOME/'sichuan_weekly_report')]:
    r = sh('git status --short', cwd=path)
    lines = [l.strip() for l in (r.stdout or '').splitlines() if l.strip()]
    code_dirty = [l for l in lines
                  if l.split()[-1].endswith(CODE_SUFFIX)
                  and not l.split()[-1].startswith(('models/', 'data/', '.', 'hermes_generated/', 'backup', 'cron/'))]
    if code_dirty:
        fail(11, name)
    print(f'[{name}] {len(lines)}变更 | 代码dirty: {"ALERT" if code_dirty else "ok"}')
    for l in code_dirty[:10]:
        print('   ALERT', l[:100])

# ═══════════════════════════════════════════
# v3 (2026-08-22): 编码/引用/数据/臆想 代码级审计
# ═══════════════════════════════════════════

sec('12. 数据完整性(特征表日期洞/NaN/价格库新鲜度)')
KNOWN_HOLES = [('2026-05-24', '2026-05-31')]  # 已知数据洞允许清单(最后有数日→恢复日), 新洞才FAIL
try:
    import pandas as _pd
    feat = _pd.read_csv(V2 / 'data' / 'features_enhanced.csv', usecols=['date'])
    _ds = sorted(feat['date'].astype(str).unique())
    _new_holes = []
    for _a, _b in zip(_ds, _ds[1:]):
        _n = (_pd.Timestamp(_b) - _pd.Timestamp(_a)).days
        if _n > 1:
            _pair = (_a, _b)
            if _pair in KNOWN_HOLES:
                print(f'INFO 已知洞: {_a}→{_b}缺{_n-1}天(允许清单)')
            else:
                _new_holes.append(f'{_a}→{_b}缺{_n-1}天')
    if _new_holes:
        fail(12, '新数据洞')
        for _h in _new_holes[:5]:
            print('  HOLE', _h)
    else:
        print('OK   特征表日期连续(已知洞已在允许清单)')
    _full = _pd.read_csv(V2 / 'data' / 'features_enhanced.csv')
    _nan_cols = [c for c in _full.columns if c not in ('date',) and _full[c].isna().any()]
    if _nan_cols:
        fail(12, '特征表NaN')
        print('  NaN列:', _nan_cols[:8])
    else:
        print('OK   特征表无NaN')
except Exception as _e12:
    fail(12, '特征表读取')
    print('  ERR', str(_e12)[:120])
try:
    import json as _j
    _ph = _j.load(open(V2 / 'output' / 'cq_price_history.json'))
    _last = max((d['date'] for d in _ph), default='')
    from datetime import date as _date
    _behind = (_date.today() - _date.fromisoformat(_last)).days if _last else 999
    if _behind > 2:
        fail(12, f'价格库滞后{_behind}天')
    print(('OK  ' if _behind <= 2 else 'FAIL') + f' 价格库最新={_last}(滞后{_behind}天, 阈值2天)')
except Exception as _e12b:
    fail(12, '价格库读取')
    print('  ERR', str(_e12b)[:120])

sec('13. 配置键审计(死配置检测, WARN级不阻断)')
try:
    import json as _j
    _cfg_files = ['v2_config.json', 'position_config.json']
    _code_files = [str(p) for p in (V2.glob('*.py'))] + [str(p) for p in (V2 / 'backtest').glob('*.py')] + \
                  [str(p) for p in (SC.glob('*.py'))]
    _code_text = ''
    for _f in _code_files:
        try:
            _code_text += open(_f, encoding='utf-8').read()
        except Exception:
            pass
    for _cf in _cfg_files:
        _p = V2 / _cf
        if not _p.exists():
            continue
        _cfg = _j.load(open(_p))
        _leaves = []
        def _walk(obj, prefix):
            if isinstance(obj, dict):
                for _k, _v in obj.items():
                    _walk(_v, f'{prefix}.{_k}' if prefix else _k)
            else:
                _leaves.append(prefix)
        _walk(_cfg, '')
        # 深度1+2的键名不在任何代码文本中 → 疑似死配置; _前缀豁免 (叶子键多为generic词如long/short, 不查)
        _dead = []
        def _walk_keys(obj, prefix, depth):
            if not isinstance(obj, dict):
                return
            for _k, _v in obj.items():
                _path = f'{prefix}.{_k}' if prefix else _k
                if depth <= 2 and not _k.startswith('_') and _k not in _code_text:
                    _dead.append(_path)
                _walk_keys(_v, _path, depth + 1)
        _walk_keys(_cfg, '', 1)
        if _dead:
            print(f'⚠ {_cf} 疑似死配置(代码零引用): {_dead}')
        else:
            print(f'OK  {_cf} 无死配置')
except Exception as _e13:
    print('ERR §13', str(_e13)[:120])

sec('15. 过期日期字面量/TODO扫描(WARN级不阻断)')
try:
    import re as _re
    from datetime import date as _date
    _stale = []
    for _f in [V2 / 'v2_daily.py', V2 / 'retrain.py', V2 / 'v2_health_report.py',
               V2 / 'position_engine.py', V2 / 'position_adapter.py']:
        if not _f.exists():
            continue
        for _i, _line in enumerate(_f.read_text(encoding='utf-8').splitlines(), 1):
            for _m in _re.findall(r'(202[0-9]-[0-9]{2}-[0-9]{2})', _line):
                try:
                    _age = (_date.today() - _date.fromisoformat(_m)).days
                    if _age > 45:
                        _stale.append(f'{_f.name}:{_i} {_m}({_age}天前)')
                except ValueError:
                    pass
    if _stale:
        print(f'⚠ 过期日期字面量(>45天): {_stale[:6]}')
    else:
        print('OK   无过期日期字面量')
    _todos = []
    for _f in [V2 / 'v2_daily.py', V2 / 'retrain.py', V2 / 'v2_health_report.py']:
        for _i, _line in enumerate(_f.read_text(encoding='utf-8').splitlines(), 1):
            if 'TODO' in _line or 'FIXME' in _line or 'XXX' in _line:
                _todos.append(f'{_f.name}:{_i}')
    if _todos:
        print(f'⚠ TODO/FIXME: {_todos[:6]}')
    else:
        print('OK   无TODO/FIXME')
except Exception as _e15:
    print('ERR §15', str(_e15)[:120])

# ── 汇总 ──
print()
print('=' * 60)
if FAILS:
    print(f'审计失败项: {len(FAILS)}')
    for s, i in FAILS:
        print(f'  §{s} {i}')
    print('EXIT CODE: 1')
    sys.exit(1)
else:
    print('全部检查通过')
    print('EXIT CODE: 0')
    sys.exit(0)
