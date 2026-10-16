#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四川电价巡检（常驻自愈）—— 价格库"不完整/静默置0"检测与修复的 cron 包装

调用 fix_price_incomplete.py（zero_sweep 工具）：
  1) 干跑近14天；无可修复项 → 静默退出（cron no_agent 空输出=不发消息）
  2) 有可修复项 → 自动 --apply 修复 → 再干跑自检 → 输出摘要（含残留检查）
用法: python3 sichuan_price_patrol.py        # 挂 cron（每日09:40）
      python3 sichuan_price_patrol.py -v     # 手动：强制输出干跑摘要
"""
import re
import subprocess
import sys

TOOL = '/home/ubuntu/sichuan_hydro_price/hermes_generated/zero_sweep_20261016/fix_price_incomplete.py'
PY = sys.executable or 'python3'
DAYS = '14'


def run(args):
    r = subprocess.run([PY, TOOL] + args, capture_output=True, text=True, timeout=900)
    return r.returncode, (r.stdout or '') + (r.stderr or '')


def parse(out):
    scan = ''
    m = re.search(r'可修复 (\d+) 天', out)
    repairable = int(m.group(1)) if m else -1
    m2 = re.search(r'(扫描 [^\n]+)', out)
    if m2:
        scan = m2.group(1)
    detail = [l.strip() for l in out.splitlines() if l.strip().startswith('✚')]
    return scan, repairable, detail


def main():
    verbose = '-v' in sys.argv
    rc, out = run(['--days', DAYS])
    if rc != 0:
        print(f'⚠️ 电价巡检: 工具执行异常 rc={rc}\n{out[-600:]}')
        return 0
    scan, repairable, detail = parse(out)
    if repairable == 0 and not verbose:
        return 0  # 干净：静默
    if repairable <= 0:
        print(f'🧾 电价巡检(手动): {scan or out[:200]}')
        return 0

    # 有可修复项 → 自动修复
    rc2, out2 = run(['--days', DAYS, '--apply'])
    _, repairable2, detail2 = parse(out2)
    rc3, out3 = run(['--days', DAYS])
    _, residual, _ = parse(out3)
    if rc2 != 0 or residual != 0:
        print(f'🔴 电价巡检: 检出{repairable}天可修复 → 修复执行异常(rc={rc2}) 或仍有残留{residual}天\n'
              f'  {scan}\n  修复输出尾: {out2[-400:]}')
    else:
        baks = re.findall(r'备份: (\S+)', out2)
        lines = [f'🧾 电价巡检(自愈): 检出{repairable}天 → 已修复{repairable2}天 → 自检残留0 ✓']
        for d in (detail2 or detail)[:6]:
            lines.append('  ' + d[:110])
        if baks:
            lines.append('  备份: ' + ' | '.join(baks[:2]))
        print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())
