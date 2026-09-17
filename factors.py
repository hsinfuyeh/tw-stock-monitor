"""L4 因子層 — 從乾淨面板算出橫斷面因子。

兩條鐵律：
  1. 所有滾動視窗只用「當日之前」的資料（rows[i-N:i]），絕不含當日。
  2. 所有篩選（流動性等）用「事件當日」的狀態判定，不用今天的 -> 否則是 look-ahead。
"""
import numpy as np
import pandas as pd
import store
from config import LIQ_MIN_AMT


# ETF 排序宇宙的次類白名單。
#   納入 domestic（台股一般型）、dividend（高股息）—— 追蹤台股、行為近似股票組合。
#   排除 leveraged/inverse：路徑相依衰減，盤整期期望報酬結構性為負，
#        放進看漲名單是主動有害。
#   排除 bond/foreign：驅動因子是美債殖利率與海外股市，完全不在 TWSE 資料裡。
#   排除 active：2025 後才出現，歷史太短無法回測。
ETF_SUBCATS = ("domestic", "dividend")


def load_panel(cat="common", subcats=None):
    """載入行情並計算除權息調整後的日總報酬。"""
    where = f"cat = '{cat}'"
    if subcats:
        lst = ", ".join(f"'{x}'" for x in subcats)
        where += f" AND subcat IN ({lst})"
    q = store.q(f"""
        SELECT date, code, name, open, high, low, close, volume, amount, cat, subcat
        FROM quotes WHERE {where} ORDER BY code, date""")
    ex = store.q("SELECT date, code, ref_price FROM exrights")
    q = q.merge(ex, on=["date", "code"], how="left")
    q = q.sort_values(["code", "date"]).reset_index(drop=True)

    g = q.groupby("code", sort=False)
    prev_close = g["close"].shift(1)
    prev_date = g["date"].shift(1)
    # 除權息日用參考價當分母 -> 得到真實總報酬
    base = q["ref_price"].where(q["ref_price"].notna(), prev_close)
    q["ret"] = q["close"] / base - 1.0
    # 停牌後復牌（間隔過大）的報酬不可信，設為缺值
    gap = (q["date"] - prev_date).dt.days
    q.loc[gap.isna() | (gap > 10), "ret"] = np.nan
    q.loc[q["ret"].abs() > 0.35, "ret"] = np.nan      # 異常值防呆（超過漲跌停幅度）
    # 調整後價格序列（總報酬指數），供多日報酬與均線使用
    q["adj"] = q.groupby("code", sort=False)["ret"].transform(
        lambda s: (1 + s.fillna(0)).cumprod())
    return q


def tick_size(p):
    """台股檔位級距。向量化。

    這個東西比看起來重要：所有固定百分比門檻都會跟檔位互動。
    例：0.2% 的十字線門檻，對股價 2400 元（檔位 5 元 = 0.208%）的股票而言
    等同於「收盤價必須完全等於開盤價」—— 差一檔就出局。同一條規則在
    不同價位的股票上嚴格程度差好幾倍，會製造出跟股價水準相關的假關聯。
    """
    p = np.asarray(p, dtype=float)
    return np.select(
        [p < 10, p < 50, p < 100, p < 500, p < 1000],
        [0.01, 0.05, 0.10, 0.50, 1.00],
        default=5.00)


def limit_prices(prev_close):
    """漲跌停價：前收 ±10%，依檔位取整（漲停無條件捨去、跌停無條件進位）。"""
    pc = np.asarray(prev_close, dtype=float)
    up_raw, dn_raw = pc * 1.10, pc * 0.90
    t_up, t_dn = tick_size(up_raw), tick_size(dn_raw)
    with np.errstate(invalid="ignore"):
        up = np.floor(up_raw / t_up + 1e-9) * t_up
        dn = np.ceil(dn_raw / t_dn - 1e-9) * t_dn
    return up, dn


def add_features(q):
    """每檔的時間序列特徵。全部 shift(1) 以確保只用當日之前的資訊。"""
    g = q.groupby("code", sort=False)

    # ---- 漲跌停旗標：鎖死時無法成交，但報酬統計假設一定成交 ----
    pc = g["close"].shift(1)
    up, dn = limit_prices(pc)
    q["limit_up"] = np.isclose(q["close"], up, rtol=0, atol=1e-6) & pc.notna()
    q["limit_dn"] = np.isclose(q["close"], dn, rtol=0, atol=1e-6) & pc.notna()
    # 鎖死（開高低收同價）代表整天沒有反向單，實務上完全買不到／賣不掉
    q["locked"] = ((q["open"] == q["close"]) & (q["high"] == q["low"])
                   & (q["limit_up"] | q["limit_dn"]))
    q["limit_20d"] = g["limit_up"].transform(
        lambda s: s.shift(1).rolling(20).sum()) + g["limit_dn"].transform(
        lambda s: s.shift(1).rolling(20).sum())

    def past(col, n, fn):
        return g[col].transform(lambda s: getattr(s.shift(1).rolling(n), fn)())

    q["amt20"]   = past("amount", 20, "mean")          # 流動性（不含當日）
    q["vol20"]   = past("volume", 20, "mean")
    q["hi20"]    = past("high", 20, "max")
    q["lo20"]    = past("low", 20, "min")
    q["sd60"]    = past("ret", 60, "std")
    q["sd20"]    = past("ret", 20, "std")

    adj = g["adj"]
    q["r5"]    = adj.transform(lambda s: s / s.shift(5)  - 1)
    q["r20"]   = adj.transform(lambda s: s / s.shift(20) - 1)
    q["r60"]   = adj.transform(lambda s: s / s.shift(60) - 1)
    q["r252"]  = adj.transform(lambda s: s / s.shift(252) - 1)
    q["mom12_1"] = adj.transform(lambda s: s.shift(21) / s.shift(252) - 1)  # 12-1 月動能
    q["vol_ratio"] = q["volume"] / q["vol20"]
    q["turn"] = q["amount"] / q["amt20"]
    return q


def eligible(q):
    """可交易宇宙：流動性 + 足夠歷史 + 非異常。用當日狀態判定。"""
    return (
        q["amt20"].notna() & (q["amt20"] >= LIQ_MIN_AMT) &
        q["sd60"].notna() & (q["sd60"] > 0) &
        q["close"].gt(0) & q["volume"].gt(0) &
        # 鎖死日無法成交 -> 那天進不了場，以該日收盤起算的報酬不可得
        ~q["locked"].fillna(False)
    )


def add_forward(q, horizons=(1, 3, 5, 10, 20)):
    """未來報酬（僅供研究/驗證，不可進入任何訊號計算）。

    必須作用在「未過濾」的完整面板上。若對已篩選的面板呼叫，shift(-h) 會跳過
    被濾掉的列，算出來的是「h 個合格觀察之後」而不是「h 個交易日之後」——
    數字看起來正常但完全是錯的。這裡主動擋下這種誤用。
    """
    if "eligible" in q.columns and not bool(q["eligible"].all()):
        raise ValueError(
            "add_forward 必須用在未過濾的面板；請改用 "
            "factors.build(horizons=...) 或先在過濾前算好前瞻報酬。")
    g = q.groupby("code", sort=False)["adj"]
    for h in horizons:
        q[f"fwd{h}"] = g.transform(lambda s: s.shift(-h) / s - 1) * 100
    return q


# ------------------------------------------------------------------ 因子定義
# 每個因子：值越大 = 預期表現越好（統一方向，方便合成）
FACTORS = {
    "反轉_5日":   lambda d: -d["r5"],
    "反轉_20日":  lambda d: -d["r20"],
    "動能_12_1":  lambda d:  d["mom12_1"],
    "低波動":     lambda d: -d["sd60"],
    "量縮":       lambda d: -d["vol_ratio"],
    "低周轉":     lambda d: -d["turn"],
    "距20日高":   lambda d: -(d["close"] / d["hi20"] - 1),   # 離高點越遠越好（反轉）
}

# ETF 只用價格類因子（見 ETF_SUBCATS 上方說明）
ETF_FACTORS = ("反轉_5日", "反轉_20日", "低波動", "量縮", "距20日高")

# ---------------------------------------------------------------------------
# 有文獻依據的因子集。
#
# 方向由既有文獻決定，不是看了本樣本的結果才決定 —— 這是重點。
# 上一版的 CORE 六個因子全是反轉類，實測單調性 -0.61、t≈-5（穩定的反指標），
# 根源是當初用 74 天的初步結果挑因子，屬於小樣本過擬合。
# 修正方式不是把符號翻轉（那同樣是用答案反推），而是改用方向已被獨立確立的因子。
#
#   動能_12_1   Jegadeesh & Titman (1993)。過去 12 個月報酬（剔除最近 1 個月）
#               為正向預測。剔除最近一個月正是為了避開短期反轉，兩者互補不衝突。
#   反轉_20日   Jegadeesh (1990)、Lehmann (1990)。一個月尺度的短期反轉，
#               在散戶佔比高的市場（台股）文獻支持較強。
#   低波動      Ang et al. (2006)、Frazzini & Pedersen (BAB)。低波動溢酬。
#   低周轉      Amihud (2002) 流動性溢酬；在散戶市場，高周轉亦為投機熱度代理。
#
# 刻意不放的：
#   反轉_5日    週尺度反轉主要來自買賣價差跳動與微結構，扣成本後難以實現。
#   距20日高    與反轉_20日高度相關，放進去等於給同一個訊號雙倍權重。
LITERATURE_FACTORS = ("動能_12_1", "反轉_20日", "低波動", "低周轉")

# 原 Skill 的六條 K 線規則，當作對照組
KLINE = {
    "K_長紅":   lambda d: ((d["close"] - d["open"]) / d["open"] >= 0.015).astype(float),
    "K_長黑":   lambda d: ((d["close"] - d["open"]) / d["open"] <= -0.015).astype(float),
    "K_十字":   lambda d: (((d["close"] - d["open"]) / d["open"]).abs() <= 0.002).astype(float),
    "K_爆量":   lambda d: (d["volume"] > d["vol20"] * 1.5).astype(float),
    "K_突破20高": lambda d: (d["close"] > d["hi20"]).astype(float),
    "K_跌破20低": lambda d: (d["close"] < d["lo20"]).astype(float),
}


def zscore_by_date(s, dates, clip=3.0):
    """橫斷面標準化。用中位數與 MAD 而非均值標準差 -> 對離群值穩健。"""
    df = pd.DataFrame({"v": s.values, "d": dates.values})
    med = df.groupby("d")["v"].transform("median")
    mad = df.groupby("d")["v"].transform(lambda x: (x - x.median()).abs().median())
    z = (df["v"].values - med.values) / (mad.values * 1.4826 + 1e-12)
    return pd.Series(np.clip(z, -clip, clip), index=s.index)


def build(cat="common", with_forward=True, subcats=None, horizons=(1, 3, 5, 10, 20),
          filter_eligible=True):
    """建面板。horizons 指定要算哪些前瞻報酬（在過濾前計算，確保是真實交易日）。

    filter_eligible=False 會保留不合格的列（仍標記 eligible 欄位）——
    漏斗頁需要這個，否則「流動性不足」那一層會顯示刷掉 0 檔，
    因為它們在更上游就已經被拿掉了，等於把最大的一刀藏起來。
    """
    q = load_panel(cat, subcats)
    q = add_features(q)
    if with_forward:
        q = add_forward(q, horizons=horizons)
    q["eligible"] = eligible(q)
    d = (q[q["eligible"]] if filter_eligible else q).copy()
    for name, fn in {**FACTORS, **KLINE}.items():
        d[name] = fn(d)
    return d


if __name__ == "__main__":
    d = build()
    print(f"可交易面板: {len(d):,} 列, {d.date.nunique()} 天, {d.code.nunique()} 檔")
    print(f"日期範圍: {d.date.min():%Y-%m-%d} ~ {d.date.max():%Y-%m-%d}")
    print(f"平均每日可交易檔數: {len(d)/d.date.nunique():.0f}")
    print("\n因子缺值率:")
    for k in list(FACTORS) + list(KLINE):
        print(f"  {k:<12} {d[k].isna().mean()*100:5.1f}%")
