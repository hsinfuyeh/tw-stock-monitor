"""L6 排序層 — 每日綜合分數與看漲/看跌名單。

刻意的設計決定：
  * 等權 z-score 合成，不做最佳化權重。樣本還不足以支撐有參數的模型，
    等權是最難過擬合的做法。
  * 名單一定帶著成績單出現。分數本身不保證有訊息量 —— 每天掃 1000 檔，
    永遠會有東西排在最前面。唯一的防線是把驗證數字印在同一頁。
  * 只納入通過驗證的因子；未通過的算出來但不計入綜合分數。
"""
import numpy as np
import pandas as pd
import factors, validate


def composite(d, use_factors, winsor=3.0):
    """等權合成。每個因子先做橫斷面穩健標準化再平均。"""
    z = pd.DataFrame(index=d.index)
    for f in use_factors:
        z[f] = factors.zscore_by_date(d[f], d["date"], clip=winsor)
    score = z.mean(axis=1, skipna=True)
    n_valid = z.notna().sum(axis=1)
    score = score.where(n_valid >= max(2, len(use_factors) // 2))
    return score, z


def risk_flags(d, panel_all):
    """風險旗標 —— 名單上每一檔都必須帶著這些資訊。"""
    f = pd.DataFrame(index=d.index)
    f["流動性"] = np.where(d["amt20"] >= 100e6, "佳",
                   np.where(d["amt20"] >= 20e6, "普通", "偏低"))
    f["近20日漲跌停"] = ""
    f["波動度"] = np.where(d["sd60"] > d["sd60"].median() * 1.5, "偏高", "正常")
    return f


def daily_ranking(d, use_factors, asof=None, top_n=20):
    asof = asof or d["date"].max()
    day = d[d["date"] == asof].copy()
    score, z = composite(d, use_factors)
    d = d.assign(score=score)
    day = d[d["date"] == asof].copy()
    day = day[day["score"].notna()].copy()
    day["pct"] = day["score"].rank(pct=True) * 100
    day["rank"] = day["score"].rank(ascending=False).astype(int)
    zz = z.loc[day.index]
    for f in use_factors:
        day[f"z_{f}"] = zz[f]
    day = day.sort_values("score", ascending=False)
    return day, day.head(top_n), day.tail(top_n).iloc[::-1]


def scorecard(d, use_factors, horizon=10):
    """綜合分數自己的成績單。這是名單能不能信的唯一依據。"""
    score, _ = composite(d, use_factors)
    p = d.assign(綜合分數=score)
    ic = validate.ic_report(p, ["綜合分數"], horizon=horizon)
    bt = validate.decile_backtest(p, "綜合分數", horizon=horizon)
    return ic, bt
