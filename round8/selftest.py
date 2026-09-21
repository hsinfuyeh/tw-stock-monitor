import sys, time
sys.path.insert(0,'.')
import numpy as np, pandas as pd
import shortterm as S, round8_study as R
d = pd.read_pickle('feat.pkl')
p = S.params()
dates = np.sort(d['date'].unique())
s0, s1 = dates[60], dates[-(int(p['hold_days'])+2)]
t0, sm0 = S.backtest(d, p, s0, s1)
t1 = R.backtest2(d, p, s0, s1)
print(len(t0), len(t1))
cols = ['signal','code','entry','exit','ret','bench','excess','why','days']
a = t0[cols].reset_index(drop=True); b = t1[cols].reset_index(drop=True)
print('identical frames:', a.equals(b))
if not a.equals(b):
    print((a['ret']-b['ret']).abs().max(), (a['bench'].fillna(0)-b['bench'].fillna(0)).abs().max(), (a['exit']!=b['exit']).sum())
# 分批版 smoke test
t2 = R.backtest2(d, p, s0, s1, staged=True)
print('staged', len(t2), R.stats(t2))
print(t2['why'].value_counts().to_dict())
# 檢查：分批版的報酬 = 手算
x=t2.iloc[0]; print(x[['code','entry','exit','why','ret','stop','target']].to_dict())
