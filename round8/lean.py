"""記憶體精簡：把 store.q 換成分塊讀取＋字串共用（intern），結果內容與原版完全相同，只是省記憶體。"""
import sys, duckdb
import pandas as pd
import store
from config import DB

def q(sql, params=None):
    con = duckdb.connect(str(DB), read_only=True)
    try:
        cur = con.execute(sql, params or [])
        parts = []
        while True:
            ch = cur.fetch_df_chunk(20)          # 20 個 vector（約 4 萬列）
            if ch is None or not len(ch):
                break
            for c in ch.columns:
                if ch[c].dtype == object:
                    ch[c] = ch[c].map(lambda s: sys.intern(s) if isinstance(s, str) else s)
            parts.append(ch)
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    finally:
        con.close()

store.q = q
