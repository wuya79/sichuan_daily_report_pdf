#!/usr/bin/env python3
"""
V2单stage编排 — v1.2 (2026-08-21, 极简版)
09:24 cron唯一入口: fetch → 特征表重建 → 35d训练 → 全量训练 → 推理出策略
设计哲学(同旧v2_daily.sh): 每步一个固定大墙(纯防挂死), 无绝对时刻/无模式判断/无让路点
  - 纯编排: retrain.py / v2_daily.py / v2_fetch_price.py 零修改, 原样调用
  - 无锁(同v2_daily.sh现状)
  - 固定大墙: fetch300/重建600/训练600/推理300 (600=旧脚本同值, 不误杀慢步骤;
    正常日实测3分钟完成, 墙纯防挂死)
  - 降级语义: 每步失败=警告+旧文件兜底=精确现状, 任何组合不劣于现状
  - 特征表完整性检查: 行数>=5000且含昨天, 异常自动从.bak恢复(copy2)
  - 真杀进程组: timeout超时killpg防孤儿python继续跑
  - 退出码: 仅推理失败exit1告警(同v3.11.2语义)
  - 推送: stdout摘要=cron推送内容(同v2_daily.sh的SUMMARY段格式)
  - 日志: /tmp/v2_single_stage.log, 启动时>500KB截断
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, date, timedelta

V2_DIR = '/home/ubuntu/v2_cq_strategy'
FETCH = '/home/ubuntu/.hermes/scripts/v2_fetch_price.py'
PH = f'{V2_DIR}/output/cq_price_history.json'
FEAT = f'{V2_DIR}/data/features_enhanced.csv'
M35 = f'{V2_DIR}/models/xgb_cls_direction_35d.json'
MFULL = f'{V2_DIR}/models/xgb_cls_direction.json'
LOG = '/tmp/v2_single_stage.log'
HERMES_PY = '/home/ubuntu/.hermes/hermes-agent/venv/bin/python3'
SEND_CSV = '/home/ubuntu/.hermes/scripts/v2_push_csv_wecom.py'
WALLS = {  # 固定大墙(秒), 纯防挂死
    'fetch': 300,
    'feat': 600,
    'train': 600,
    'infer': 300,
    'send_csv': 90,
}
FEAT_MIN_ROWS = 5000  # 特征表完整性: 最小行数


def log(msg):
    """技术日志只写文件不外发(用户铁律); V2_STAGE_VERBOSE=1时同步print(手动调试用)
    cron推送内容=stdout, 只允许摘要+警告行"""
    line = f'[{datetime.now():%H:%M:%S}] {msg}'
    if os.environ.get('V2_STAGE_VERBOSE') == '1':
        print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def rotate_log():
    try:
        if os.path.getsize(LOG) > 500 * 1024:
            with open(LOG, 'rb') as f:
                f.seek(-300 * 1024, os.SEEK_END)
                tail = f.read().decode(errors='ignore')
            with open(LOG, 'w') as f:
                f.write(f'[log截断] {datetime.now()}\n' + tail)
    except OSError:
        pass


def run_step(cmd, wall_s, tag):
    if os.environ.get('V2_STAGE_DRY') == '1':
        log(f'▶ {tag} [DRY] {cmd} (墙{wall_s}s)')
        return 0, ''
    log(f'▶ {tag} (墙{wall_s}s)')
    t0 = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=V2_DIR, timeout=wall_s,
                           capture_output=True, text=True,
                           start_new_session=True)
        dt = time.time() - t0
        out = (p.stdout or '') + (p.stderr or '')
        with open(LOG, 'a') as f:
            f.write(out + '\n')
        log(f'✔ {tag} exit={p.returncode} 耗时{dt:.0f}s')
        return p.returncode, out
    except subprocess.TimeoutExpired as e:
        # 真杀进程组: 防孤儿python继续跑(如retrain在超时后仍写文件污染后续步骤)
        try:
            pid = getattr(e, 'pid', None)
            if pid:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass
        out = getattr(e, 'stdout', None)
        out = out.decode(errors='ignore') if isinstance(out, bytes) else (out or '')
        with open(LOG, 'a') as f:
            f.write(out + '\n')
        log(f'✘ {tag} 超时({wall_s}s)已强杀进程组')
        return 124, out


def check_d1_in_price():
    """价格库是否含完整D-1(24h da/rt全有值且不全0, 同fetch_price的_is_complete)"""
    try:
        with open(PH) as f:
            ph = json.load(f)
    except Exception:
        return False
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    for rec in ph:
        if rec.get('date') != yesterday:
            continue
        ok = True
        for key in ('da', 'rt'):
            arr = rec.get(key) or []
            if len(arr) != 24 or any(v is None for v in arr):
                ok = False
            if all(v == 0 for v in arr):
                ok = False
        return ok
    return False


def check_feat_integrity():
    """特征表行数>=5000且日期>=价格库最新日期-1天; 异常自动从.bak恢复
    参照价格库而非'昨天': fetch失败日特征表本来就只能到D-2, 不该误判"""
    ok = False
    try:
        import pandas as pd
        df = pd.read_csv(FEAT)
        if len(df) >= FEAT_MIN_ROWS:
            dates = sorted(df['date'].astype(str))
            min_ok_date = None
            try:
                with open(PH) as f:
                    ph = json.load(f)
                last_price = max(r['date'] for r in ph)
                min_ok_date = (date.fromisoformat(last_price)
                               - timedelta(days=1)).isoformat()
            except Exception:
                pass
            if min_ok_date is None:
                ok = True  # 价格库不可读时不拦截
            else:
                ok = bool(dates) and dates[-1] >= min_ok_date
    except Exception:
        ok = False
    if ok:
        return True
    bak = FEAT + '.bak'
    if os.path.exists(bak):
        try:
            shutil.copy2(bak, FEAT)
            log('⚠ 特征表异常(行数/日期), 已从.bak恢复')
        except Exception:
            log('⚠ 特征表异常且.bak恢复失败')
    else:
        log('⚠ 特征表异常且无.bak可用')
    return False


def newer_than_start(path, t0):
    try:
        return os.path.getmtime(path) > t0
    except OSError:
        return False


def main():
    rotate_log()
    t0 = time.time()
    warns = []
    log(f'═══ v2_single_stage 启动 {datetime.now():%Y-%m-%d %H:%M:%S} ═══')

    # ── 1. fetch价格 ──
    rc, out = run_step(f'python3 {FETCH}', WALLS['fetch'], 'fetch价格')
    if rc != 0:
        warns.append('⚠️ 价格补拉失败/超时, 特征表将不含D-1(同现状)')
    log(f'   价格库D-1完整: {check_d1_in_price()}')

    # ── 2. 特征表重建(含D-1若拉到) ──
    rc, out = run_step('python3 retrain.py --features-only', WALLS['feat'],
                       '特征表重建')
    if rc != 0:
        warns.append('⚠️ 特征重建未完成, 后续训练将自行尝试重建(同现状兜底)')
    if not check_feat_integrity():  # N14: 重建后立即检查, 防坏表流入训练
        warns.append('⚠️ 特征表完整性检查未通过(可能已从.bak恢复), 请人工核查')
    log(f'   特征表已更新: {newer_than_start(FEAT, t0)}')

    # ── 3. 35d训练(窗口含D-1) ──
    rc, out = run_step('python3 retrain.py --train-days 35', WALLS['train'],
                       '35d训练')
    if rc != 0:
        warns.append('⚠️ 35d重训失败/超时, 用旧35d模型(同现状)')
    if not check_feat_integrity():
        warns.append('⚠️ 35d训练后特征表完整性复查未通过')

    # ── 4. 全量训练(窗口含D-1) ──
    rc, out = run_step('python3 retrain.py', WALLS['train'], '全量训练')
    if rc != 0:
        warns.append('⚠️ 全量重训失败/超时, 用旧全量模型(同现状)')
    if not check_feat_integrity():
        warns.append('⚠️ 全量训练后特征表完整性复查未通过')
    log(f'   模型更新: 35d={newer_than_start(M35, t0)} '
        f'全量={newer_than_start(MFULL, t0)}')

    # ── 5. 推理出策略(必达, 用当前最好的文件组合) ──
    rc, out = run_step('python3 v2_daily.py', WALLS['infer'], '推理出策略')
    if rc != 0:
        log('❌ 推理失败')
        for w in warns:  # N7: 失败推送也带上降级警告
            print(w)
        print('❌ 策略生成失败(exit %d)，技术日志尾部:' % rc)
        print((out or '')[-1500:])
        return 1

    # ── 5.5 CSV原生附件推送企微(2026-08-26新增; 失败仅警告, 摘要/链接不受影响) ──
    if os.path.exists(SEND_CSV):
        rc_send, _ = run_step(
            f'export $(grep -E "^WECOM_" /home/ubuntu/.hermes/.env | xargs) '
            f'&& {HERMES_PY} {SEND_CSV}',
            WALLS['send_csv'], 'CSV附件推送企微')
        if rc_send != 0:
            warns.append('⚠️ CSV附件推送企微失败(公网链接仍可用)')
    else:
        log('⚠ SEND_CSV不存在, 跳过附件推送')
        warns.append('⚠️ v2_push_csv_wecom.py缺失, CSV附件未推送')

    # 推送摘要(同v2_daily.sh格式: SUMMARY段+警告行)
    summary = []
    in_sum = False
    for ln in (out or '').splitlines():
        if ln.startswith('===SUMMARY==='):
            in_sum = True
            continue
        if ln.startswith('===END==='):
            break
        if in_sum and not ln.startswith('==='):
            summary.append(ln)
    if not summary:  # N8: 摘要缺失时fallback, 绝不空推送
        summary = ['⚠️ 策略已生成但摘要提取失败, 请检查reports目录最新CSV']
    for ln in summary:
        print(ln)
    for w in warns:
        print(w)
    log(f'═══ 完成 {datetime.now():%H:%M:%S} 策略已出 ═══')
    return 0


if __name__ == '__main__':
    sys.exit(main())
