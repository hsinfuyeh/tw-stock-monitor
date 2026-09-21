"""建立特徵面板 feat.pkl（第 8 輪用）。

重現方式（需要約 8 GB 記憶體；本機 4 GB 會被系統砍掉，所以用 lean.py 分塊讀取並共用字串）：
  1. 在專案資料夾：python -c "import json,ingest,revenue,pandas as pd; ..."
     -> 以 raw/_calendar_hist.json 取代 ingest.trading_calendar 後呼叫 revenue.load()，存成 round8/revenue_load.pkl
        （不能直接呼叫原本的 trading_calendar：它會重抓當月並覆寫 raw/_calendar.json）
  2. python build_feat.py      -> feat.pkl（4,325,998 列，2008-01-02 ~ 2026-09-18）
  3. python selftest.py        -> 三個開關全關時，逐筆結果必須跟 shortterm.backtest 完全一致
  4. python run_h.py           -> H1–H4（round8_results.json）
  5. python run_sim.py         -> 組合模擬（round8_sim.csv）
版本：pandas 2.3.3、numpy 2.2.x、duckdb 1.5.x。
"""
import sys, time, json, resource
sys.path.insert(0, '.')
import pandas as pd, numpy as np
import lean                                   # 先換掉 store.q
import revenue
_rv = pd.read_pickle('round8/revenue_load.pkl')
revenue.load = lambda codes=None: _rv
import shortterm
t0=time.time()
d = shortterm.features()
print('features rows', len(d), d['date'].min(), d['date'].max(), 'sec', round(time.time()-t0), 'peak MB', round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024), flush=True)
print(list(d.columns), flush=True)
d.to_pickle('feat.pkl')
print('saved', flush=True)
