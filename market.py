"""大盤頁（site/market.html）：整個市場的狀態，每個數字都附「在近 10 年排第幾」。

仿照 ToAlpha 大盤頁的做法：數字不單獨出現，一定附脈絡（近 10 年百分位、近一年走勢）。
全部用倉儲裡現成的資料算，不另外抓：
    加權指數        market_index（MI_INDEX 原始檔解析，2009-01-05 起）
    等權平均        quotes：上市普通股每天漲跌幅的平均（每檔一樣重），跟加權指數比，
                    看是權值股在撐盤、還是多數股票都在漲
    市場寬度        上漲家數比例、站上季線（60 日均線）的比例、創 20 日新高減新低的家數
    本益比          valuation：有獲利（本益比 > 0）的上市普通股的本益比中位數
    融資餘額        margin_total：證交所公布的全市場融資金額（換成億元）

只是事實，不是預測：這頁沒有任何「該買／該賣」的意思，「偏高」也不代表會跌。
"""
import numpy as np
import pandas as pd

import store

YEARS = 10              # 百分位用近幾年
TAIEX = "發行量加權股價指數"


def _pct_rank(s, v):
    """v 在 s 裡排第幾百分位（0–100）。"""
    s = pd.Series(s).dropna()
    if not len(s) or v is None or not np.isfinite(v):
        return None
    return round(float((s < v).mean() * 100))


def series():
    """逐日的市場指標（DataFrame，以日期為索引）。"""
    start = "DATE '{}-01-01'".format(pd.Timestamp.today().year - YEARS - 1)
    q = store.q("""
        WITH c AS (
          SELECT date, code, close, is_exdiv,
                 LAG(close) OVER (PARTITION BY code ORDER BY date) AS prev,
                 AVG(close) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma60,
                 COUNT(*) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS n60,
                 MAX(close) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS hi20,
                 MIN(close) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS lo20
          FROM quotes WHERE cat = 'common' AND date >= {s} - INTERVAL 120 DAY)
        SELECT date,
               COUNT(*) AS n,
               AVG(CASE WHEN NOT is_exdiv AND prev > 0 THEN close / prev - 1 END) AS ew_ret,
               AVG(CASE WHEN prev > 0 THEN (close > prev)::INT END) AS up_share,
               AVG(CASE WHEN n60 = 60 THEN (close > ma60)::INT END) AS above_ma60,
               SUM((close > hi20)::INT) - SUM((close < lo20)::INT) AS hi_lo
        FROM c WHERE date >= {s} GROUP BY date ORDER BY date""".format(s=start))
    q["date"] = pd.to_datetime(q["date"])
    d = q.set_index("date")
    pe = store.q("""SELECT v.date, MEDIAN(v.pe) AS pe FROM valuation v
                    JOIN quotes q ON q.date = v.date AND q.code = v.code AND q.cat = 'common'
                    WHERE v.pe > 0 AND v.date >= {s} GROUP BY v.date""".format(s=start))
    # 官方的融資金額（仟元 -> 億元）。不要用個股融資張數 × 收盤價：那是部位市值，約是借款的兩倍
    mg = store.q("""SELECT date, margin_amt * 1000 / 1e8 AS margin_yi FROM margin_total
                    WHERE date >= {s}""".format(s=start))
    ix = store.q("""SELECT date, close AS taiex FROM market_index WHERE name = '{n}' AND date >= {s}"""
                 .format(n=TAIEX, s=start))
    for x in (pe, mg, ix):
        x["date"] = pd.to_datetime(x["date"])
        d = d.join(x.set_index("date"), how="left")
    return d.sort_index()


METRICS = [
    # (欄位, 名稱, 單位, 白話說明)
    ("above_ma60", "站上季線的比例", "%", "上市普通股裡，收盤在 60 日均線之上的比例。越高代表越多股票處在上升趨勢。"),
    ("up_share", "今天上漲的比例", "%", "上市普通股裡今天收漲的比例。"),
    ("hi_lo", "創新高減創新低", "家", "收盤創 20 日新高的家數，減掉創 20 日新低的家數。正的越多，代表強勢股越多。"),
    ("pe", "本益比中位數", "倍", "有獲利的上市普通股，本益比排在正中間的那個數字。越高代表整體股價相對獲利越貴。"),
    ("margin_yi", "融資餘額", "億元", "投資人向券商借錢買股票、還沒還的總金額（證交所公布的融資金額）。快速增加常代表散戶追價的熱度。"),
]


def compute():
    """market.json：今天的數字、近 10 年百分位、近一年走勢，以及加權 vs 等權。"""
    d = series()
    if d.empty:
        return None
    last = d.index.max()
    win = d[d.index > last - pd.DateOffset(years=YEARS)]
    yr = d[d.index > last - pd.DateOffset(years=1)]
    out = {"date": str(last)[:10], "years": YEARS, "since": str(win.index.min())[:10], "metrics": []}
    for col, name, unit, desc in METRICS:
        s = win[col]
        v = s.iloc[-1] if len(s) else np.nan
        scale = 100 if unit == "%" else 1
        out["metrics"].append({
            "key": col, "name": name, "unit": unit, "desc": desc,
            "value": None if pd.isna(v) else round(float(v) * scale, 1),
            "pct10y": _pct_rank(s, v),
            "spark": [None if pd.isna(x) else round(float(x) * scale, 2) for x in yr[col].tolist()],
        })
    # 加權 vs 等權：近一年，兩條都從 100 開始
    t = yr["taiex"]
    ew = (1 + yr["ew_ret"].fillna(0)).cumprod()
    out["vs"] = {
        "dates": [str(x)[:10] for x in yr.index],
        "taiex": [None if pd.isna(x) else round(float(x / t.dropna().iloc[0] * 100), 2) for x in t],
        "equal": [round(float(x / ew.iloc[0] * 100), 2) for x in ew],
    }
    tx = d["taiex"].dropna()
    if len(tx) >= 2:
        out["taiex"] = {"close": round(float(tx.iloc[-1]), 2), "change": round(float(tx.iloc[-1] - tx.iloc[-2]), 2),
                        "pct10y": _pct_rank(win["taiex"], tx.iloc[-1])}
    tv, ev = out["vs"]["taiex"][-1], out["vs"]["equal"][-1]
    out["vs"]["gap"] = None if tv is None else round(tv - ev, 1)
    return out


if __name__ == "__main__":
    import json
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    r = compute()
    print(json.dumps({k: v for k, v in r.items() if k != "vs"}, ensure_ascii=False, indent=1)[:3000])
    print("加權 vs 等權（近一年，起點 100）：", r["vs"]["taiex"][-1], r["vs"]["equal"][-1], "差", r["vs"]["gap"])
