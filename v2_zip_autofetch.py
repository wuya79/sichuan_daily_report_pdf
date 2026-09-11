#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V2 zip包自动拉取 — OSS官方包 探测→下载→解包→兜底缓存重建 (2026-09-11)
职责: 自动把 OSS 新发布的 {YYYYMMDD}.zip (前日实际+D日前+D+1预测) 拉取并解包到
      ~/data_archive/supply_extracted/z{YYYYMMDD}/, 再重建 data/supply_fallback.json (读取-合并-写回)。
静默: 无新包/无动作 → 空stdout (no_agent不打扰); 有新包处理结果/异常 → 输出投递。
安全: HEAD探测→临时文件下载→zip有效性校验→路径穿越防护→临时目录解包后rename; 状态去重, 失败下次重试。
参数: --dry                 全流程使用 /tmp/zip_autofetch_test/ 沙箱(不碰真实目录与fb)
      --force-d8 YYYYMMDD   强制处理指定日期(绕过状态与目录占位)
      --backfill A [B]      历史区间回填(A~B含端点, 缺B=单日; 末尾统一重建fb并输出报告)
"""
import json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.request, zipfile
from datetime import date, timedelta

HOME = os.path.expanduser('~')
REPO = os.path.join(HOME, 'v2_cq_strategy')
BASE_URL = 'https://electricity-bill.oss-cn-chengdu.aliyuncs.com/chongqing'
DRY = '--dry' in sys.argv
FORCE = None
if '--force-d8' in sys.argv:
    FORCE = sys.argv[sys.argv.index('--force-d8') + 1]
BACKFILL = None
if '--backfill' in sys.argv:
    _i = sys.argv.index('--backfill')
    _a = sys.argv[_i + 1]
    _b = sys.argv[_i + 2] if len(sys.argv) > _i + 2 and not sys.argv[_i + 2].startswith('--') else _a
    BACKFILL = (_a, _b)

if DRY:
    ROOT = '/tmp/zip_autofetch_test'
    ZIP_DIR = os.path.join(ROOT, 'zips')
    EXT_DIR = os.path.join(ROOT, 'extracted')
    STATE = os.path.join(ROOT, 'state.json')
    FB_OUT = os.path.join(ROOT, 'supply_fallback.test.json')
else:
    ZIP_DIR = os.path.join(HOME, 'data_archive', 'supply_zips')
    EXT_DIR = os.path.join(HOME, 'data_archive', 'supply_extracted')
    STATE = os.path.join(HOME, '.hermes', 'scripts', 'zip_autofetch_state.json')
    FB_OUT = None


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def check(d8):
    try:
        req = urllib.request.Request(f'{BASE_URL}/{d8}.zip', method='HEAD')
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, int(r.headers.get('Content-Length') or 0)
    except urllib.error.HTTPError as e:
        return e.code, 0
    except Exception:
        return None, 0


def download(d8):
    os.makedirs(ZIP_DIR, exist_ok=True)
    dst = os.path.join(ZIP_DIR, f'{d8}.zip')
    if os.path.exists(dst) and zipfile.is_zipfile(dst):
        return dst, os.path.getsize(dst)
    tmp = dst + '.tmp'
    with urllib.request.urlopen(f'{BASE_URL}/{d8}.zip', timeout=180) as r, open(tmp, 'wb') as f:
        shutil.copyfileobj(r, f)
    if os.path.getsize(tmp) < 10_000 or not zipfile.is_zipfile(tmp):
        os.remove(tmp)
        raise ValueError('下载文件非有效zip或过小')
    os.replace(tmp, dst)
    return dst, os.path.getsize(dst)


def extract(d8, zp):
    dest = os.path.join(EXT_DIR, f'z{d8}')
    tmpd = dest + '.tmpdir'
    if os.path.isdir(tmpd):
        shutil.rmtree(tmpd)
    os.makedirs(tmpd, exist_ok=True)
    with zipfile.ZipFile(zp) as z:
        for n in z.namelist():
            if n.startswith('/') or '..' in n.split('/'):
                raise ValueError(f'zip含不安全路径: {n}')
        z.extractall(tmpd)
    nfiles = sum(1 for _, _, fs in os.walk(tmpd) for f in fs if f.endswith('.xlsx'))
    if nfiles < 5:
        shutil.rmtree(tmpd)
        raise ValueError(f'解包文件数异常: {nfiles}')
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.replace(tmpd, dest)
    return dest, nfiles


def rebuild_fb():
    cmd = ['python3', os.path.join(REPO, 'build_supply_fallback.py')]
    if FB_OUT:
        cmd.append(FB_OUT)
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=300)
    out = (r.stdout or '') + (r.stderr or '')
    m = re.search(r'校验失败项[:：]\s*(.+)', out)
    vline = (m.group(1).strip() if m else '未找到校验行(!)')
    tail = out.strip().splitlines()[-1] if out.strip() else ''
    return r.returncode, vline, tail


def main():
    os.makedirs(ZIP_DIR, exist_ok=True)
    os.makedirs(EXT_DIR, exist_ok=True)
    st = _load_state()
    msgs = []
    if FORCE:
        cands = [FORCE]
    elif BACKFILL:
        _s = date(int(BACKFILL[0][:4]), int(BACKFILL[0][4:6]), int(BACKFILL[0][6:8]))
        _e = date(int(BACKFILL[1][:4]), int(BACKFILL[1][4:6]), int(BACKFILL[1][6:8]))
        cands = [(_s + timedelta(days=k)).strftime('%Y%m%d') for k in range((_e - _s).days + 1)]
    else:
        today = date.today()
        cands = [(today - timedelta(days=k)).strftime('%Y%m%d') for k in range(0, 4)]
    new_extract = False
    n_dl = n_skip = 0
    missing = []
    for d8 in cands:
        if not FORCE and st.get(d8, {}).get('status') == 'done':
            continue
        dest = os.path.join(EXT_DIR, f'z{d8}')
        # 既有解包目录(历史手工解包/前次成功): >=15个xlsx 视为已完成
        if not FORCE and os.path.isdir(dest):
            n = sum(1 for _, _, fs in os.walk(dest) for f in fs if f.endswith('.xlsx'))
            if n >= 15:
                st[d8] = {'status': 'done', 'note': 'pre-extracted', 'files': n, 'at': _now()}
                n_skip += 1
                continue
        code, size = check(d8)
        if code != 200:
            st[d8] = {'status': f'http{code}', 'at': _now()}
            if BACKFILL:
                missing.append(f'{d8}:{code}')
            continue
        try:
            zp, zsize = download(d8)
            dest, n = extract(d8, zp)
            st[d8] = {'status': 'done', 'zip_bytes': zsize, 'files': n, 'at': _now()}
            new_extract = True
            n_dl += 1
            msgs.append(f'✅ OSS {d8}.zip 已拉取+解包: {zsize/1024:.0f}KB, {n}文件 → z{d8}/')
        except Exception as e:
            st[d8] = {'status': 'error', 'err': str(e)[:200], 'at': _now()}
            msgs.append(f'⚠ {d8}.zip 处理失败: {e}')
    if new_extract or BACKFILL:
        try:
            rc, vline, tail = rebuild_fb()
            okmark = '✅' if (rc == 0 and vline == '无') else '⚠'
            msgs.append(f'{okmark} 兜底缓存重建(读取-合并-写回): 校验失败项={vline}')
            if rc != 0 or vline != '无':
                msgs.append(f'   builder尾行: {tail[:200]}')
        except Exception as e:
            msgs.append(f'⚠ 兜底缓存重建异常: {e}')
    if BACKFILL:
        msgs.append(f'回填报告 {BACKFILL[0]}~{BACKFILL[1]}: 共{len(cands)}天 | 新处理{n_dl} | 已有跳过{n_skip} | 缺包{len(missing)}'
                    + (f' [{", ".join(missing[:10])}{"..." if len(missing) > 10 else ""}]' if missing else ''))
    _save_state(st)
    if msgs:
        print('\n'.join(msgs))


if __name__ == '__main__':
    main()
