#!/usr/bin/env python3
"""一次性验证: 推理输入快照旁路重跑(2026-09-11生产, target_date=2026-09-12)
原因: 09-08/09-09 首帧均遇交易中心改版数据缺失(degraded帧, da_lag1=0), 验证强度不足, 顺延至09-11健康帧
判定: 文件存在 + X shape 24x54 + 与archive feat_snapshot摘要三列一致(差<0.01)
成功exit 0输出明细; 失败exit 1(触发告警)
"""
import json, os, sys
import numpy as np

BASE = '/home/ubuntu/v2_cq_strategy'
D = '2026-09-12'
rp = os.path.join(BASE, 'output', 'replay_inputs', f'replay_input_{D}.json')
ap = os.path.join(BASE, 'output', 'archive', f'ml_strategy_{D}.json')

if not os.path.exists(rp):
    print(f'❌ 快照未生成: {rp}')
    print(f'   archive存在: {os.path.exists(ap)}')
    print('   可能原因: ①diff A+B未生效(v2_daily.py无旁路) ②今日model_ok=False/P0兜底(查archive model_version) ③上游数据仍缺失(交易中心改版未恢复)')
    sys.exit(1)
if not os.path.exists(ap):
    print(f'⚠ archive缺失(今日生产异常?): {ap}')
    sys.exit(1)

r = json.load(open(rp)); a = json.load(open(ap))
X = np.array(r['X'])
problems = []
if X.shape != (24, 54):
    problems.append(f'shape={X.shape} 期望24x54')
if len(r['features']) != 54:
    problems.append(f'n_features={len(r["features"])}=54?')
f = r['features']
for col, sk in [('da_lag1', 'da_lag1'), ('rt_lag1', 'rt_lag1'), ('market_ratio_d1', 'market_ratio_d1')]:
    cm = float(X[:, f.index(col)].mean())
    sv = float(a.get('feat_snapshot', {}).get(sk, float('nan')))
    d = abs(cm - sv)
    if d >= 0.01:
        problems.append(f'{col}差{d:.4f}')
    print(f'{"✅" if d < 0.01 else "❌"} {col}: X均值={cm:.4f} vs archive摘要={sv:.4f} (差{d:.4f})')

print(f'model_version={r.get("model_version")} data_quality={r.get("data_quality")} rows={r.get("n_rows")} cols={r.get("n_cols")}')
if problems:
    print('❌ 未通过: ' + '; '.join(problems))
    sys.exit(1)
print('✅ 快照旁路验证通过: 24x54, X与archive摘要一致 = 快照即生产真实输入')
