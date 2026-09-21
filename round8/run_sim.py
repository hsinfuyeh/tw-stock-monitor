import sys, json, time, itertools
sys.path.insert(0,'.')
import numpy as np, pandas as pd
import shortterm as S, round8_sim as M
d = pd.read_pickle('feat.pkl')
arr = S._forward(d)
dates = np.sort(d['date'].unique())
trades = pd.read_pickle('round8_trades.pkl')
scen = {'A Arm0(設目標)': trades['Arm0 基準'], 'B Arm1(不設目標)': trades['Arm1 不設目標'], 'C Arm0+大盤閘門(描述用)': trades['H1 大盤閘門']}
T_days = len(dates)
first = int(np.searchsorted(pd.DatetimeIndex(dates), pd.Timestamp('2008-04-07')))
years = (dates[-1]-dates[first]).astype('timedelta64[D]').astype(int)/365.25
print('days', T_days, 'first', first, 'years', round(years,2), flush=True)

prepared = {}
for k,t in scen.items():
    t0=time.time()
    T, paths = M.prep(t, arr, dates)
    mx, av = M.check_paths(T, paths)
    print(k, 'trades', len(T), 'path-vs-ret max err %.4f mean %.5f (單位 %%)'%(mx, av), 'sec', round(time.time()-t0), flush=True)
    prepared[k]=(T,paths)

# 0050 買進持有（同期間）
bo, bc = S._bench(d)
bser = bc.reindex(pd.DatetimeIndex(dates)).ffill()
beq = (bser/bser.iloc[first]).to_numpy(); beq[:first]=1.0
bex = np.zeros(T_days)
bm = M.metrics(beq, bex, 0, 0, first, years)
print('0050 buy&hold:', {k:v for k,v in bm.items() if k in ('總報酬%','年化%','最大回落%','10日為正%','10日最差%')}, flush=True)

rows=[]
grid = list(itertools.product([3,5,8,10],[0.005,0.01,0.015,0.02],[1,2]))
for k,(T,paths) in prepared.items():
    for N,r,npd in grid:
        t0=time.time()
        eq,expo,taken,win = M.simulate(T,paths,T_days,N,r,npd)
        m = M.metrics(eq,expo,taken,win,first,years)
        m.update(M.boot(eq,first))
        m.update({'情境':k,'N':N,'r%':r*100,'單日新增':npd})
        rows.append(m)
    print(k,'done',flush=True)
R = pd.DataFrame(rows)
R.to_pickle('round8_sim.pkl'); R.to_csv('round8_sim.csv', index=False)
pd.set_option('display.width',250); pd.set_option('display.max_columns',30)
cols=['N','r%','單日新增','年化%','總報酬%','最大回落%','10日為正%','平均曝險%','成交筆數','250日虧損機率%','250日回落95分位%']
for k in scen:
    print('\n=====',k,'=====')
    print(R[R['情境']==k][cols].to_string(index=False))
json.dump({'bh0050':bm}, open('round8_bh.json','w'), ensure_ascii=False)
