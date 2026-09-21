import sys, json, time
sys.path.insert(0,'.')
import numpy as np, pandas as pd
import shortterm as S, round8_study as R
d = pd.read_pickle('feat.pkl')
p = S.params()
dates = np.sort(d['date'].unique())
s0, s1 = dates[60], dates[-(int(p['hold_days'])+2)]
arr, bench = S._forward(d), S._bench(d)

# H1 大盤閘門：訊號日 0050 收盤 < 其 MA60（用原始收盤，跟網站的均線一致）
b = d[d['code']=='0050'].set_index('date')['close']
ma60 = b.rolling(60).mean()
weak = (b < ma60)                      # NaN 比較為 False -> 不排除
gate_ok = ~weak.reindex(d['date']).fillna(False).to_numpy()
mask_h1 = pd.Series(gate_ok, index=d.index)
# H2 追高上限
ext = (d['close'] - d['ma20']) / d['atr14']
mask_h2 = pd.Series((ext <= 3).to_numpy() | ext.isna().to_numpy(), index=d.index)

arms = {
 'Arm0 基準':      dict(),
 'Arm1 不設目標':  dict(_p=dict(use_target=0)),
 'H1 大盤閘門':    dict(pool_mask=mask_h1),
 'H2 追高上限':    dict(pool_mask=mask_h2),
 'H3 分批停利':    dict(staged=True),
 'H4 跳空上限':    dict(gap_cap=True),
}
res, trades = {}, {}
for name, kw in arms.items():
    kw = dict(kw); pp = S.params(**kw.pop('_p')) if '_p' in kw else p
    t0=time.time()
    t = R.backtest2(d, pp, s0, s1, arr, bench, **kw)
    trades[name] = t
    res[name] = R.by_period(t)
    print(name, 'sec', round(time.time()-t0), flush=True)
    for per, r in res[name].items(): print('   ', per, r, flush=True)
base = trades['Arm0 基準']
pair = {}
for name, t in trades.items():
    if name.startswith('Arm0'): continue
    pair[name] = {per: R.paired(t, base, a, bb) for per, a, bb in R.PERIODS}
    print('paired', name, pair[name], flush=True)
json.dump({'res':res,'paired':pair}, open('round8_results.json','w'), ensure_ascii=False, indent=1, default=str)
pd.to_pickle(trades, 'round8_trades.pkl')
# H1/H2 覆蓋：被排除的訊號比例
print('gate weak-day share (all dates):', round(float(weak.reindex(pd.DatetimeIndex(dates)).fillna(False).mean()),3))
print('H2 pool excluded share among score>=50 rows:', round(float(1-mask_h2[S.score(d, p)['liq']].mean()),3))
