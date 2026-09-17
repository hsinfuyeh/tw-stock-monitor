"""歷史勝率分級（likelihood tiers）。

定位 —— 這不是預測，是查表：
    「歷史上條件相似的案例中，有多少比例在後續 N 日跑贏市場中位數？」
    這是可驗證的歷史頻率，不是對未來的機率宣稱。

為什麼用「勝率」而不是「預期報酬」：
    實測顯示殖利率改善的是<中位數>而非<平均數> —— 低殖利率股贏的次數少
    但偶爾大贏，肥尾把平均拉平。散戶不會持有夠多檔去捕捉那條尾巴，
    所以「贏的次數」才是會實際遇到的結果。用勝率排序與證據方向一致。

只用通過驗證的成分：
    殖利率位階   單獨的勝率梯度 +7.93pp (t=5.26)
    價格位置     單獨的勝率梯度 +2.28pp (t=1.62)
    兩者等權     46.2% -> 53.6% (10日)，梯度 +6.84pp (t=4.40)，五級全單調

刻意不放進來的：
    六種 K 線型態  資訊量接近零
    綜合評分       反指標 (t=-4.44)
    法人買超       對勝率的梯度 -0.49pp (t=-0.56)，毫無貢獻
"""
import numpy as np
import pandas as pd

import validate

# 只保留對「勝率」有實證貢獻的成分。
# 法人買超刻意不放：它驗證過的是十分位平均的單調性（+0.88），
# 那是另一個指標；實測它對勝率的梯度是 -0.49pp（t=-0.56），毫無貢獻。
# 用一個對本指標無效的成分做等權合成，只會稀釋訊號。
COMPONENTS = ["殖利率位階", "價格位置"]


def add_components(d):
    """在面板上補齊各成分的當日橫斷面百分位。"""
    g = d.groupby("date")
    if "ypct" not in d:
        d["ypct"] = g["div_yield"].rank(pct=True)
    if "ppct" not in d:
        d["ppct"] = g["pos252"].rank(pct=True)
    if "ipct" not in d and "法人買超" in d:
        d["ipct"] = g["法人買超"].rank(pct=True)
    return d


def score(d, weights=(1.0, 1.0, 0.0)):
    """成分等權合成（0~1）。

    等權是刻意的：目前沒有足夠證據支撐任何一組最佳化權重，
    而最佳化權重正是最容易過擬合的地方。
    （實測「殖利率權重加倍」梯度更高，+8.28pp vs +6.84pp —— 但那是看到
      結果才調的權重，不採用。）
    """
    cols = ["ypct", "ppct", "ipct"]
    have = [(c, w) for c, w in zip(cols, weights) if c in d.columns]
    num = sum(d[c].fillna(0.5) * w for c, w in have)
    den = sum(w for _, w in have)
    return num / den


def hit_rate_table(d, horizon=10, n_tier=5, min_n=200):
    """把分數切成 N 個等級，統計每級的歷史勝率。

    勝率定義：後續 horizon 日報酬 > 當日全市場中位數。
    用中位數而非平均數當基準，因為橫斷面右偏，平均數會被少數暴漲股拉高。
    """
    fw = "fwd{}".format(horizon)
    x = d.dropna(subset=[fw, "L"]).copy()
    med = x.groupby("date")[fw].transform("median")
    x["win"] = (x[fw] > med).astype(float)
    x["tier"] = x.groupby("date")["L"].transform(
        lambda s: pd.qcut(s.rank(method="first"), n_tier, labels=False, duplicates="drop"))
    rows = []
    for t, g in x.groupby("tier"):
        if len(g) < min_n:
            continue
        p = float(g["win"].mean())
        n = len(g)
        # 以交易日為聚類單位算有效樣本，避免把同一天的上千檔當成獨立觀察
        eff = g["date"].nunique() / horizon
        se = np.sqrt(p * (1 - p) / max(eff, 1))
        rows.append(dict(等級=int(t) + 1, 樣本數=n, 交易日=g["date"].nunique(),
                         勝率=round(p * 100, 1),
                         區間下限=round((p - 1.96 * se) * 100, 1),
                         區間上限=round((p + 1.96 * se) * 100, 1),
                         中位超額=round(float((g[fw] - med.loc[g.index]).median()), 3)))
    return pd.DataFrame(rows)


def validate_gradient(d, horizon=10, n_tier=5):
    """檢定「等級越高勝率越高」這件事本身是否顯著。

    做法：每一期算「最高級勝率 − 最低級勝率」，對這條時間序列做檢定。
    這樣才是把交易日當成獨立單位，而不是把同日的上千檔當獨立樣本。
    """
    fw = "fwd{}".format(horizon)
    x = d.dropna(subset=[fw, "L"]).copy()
    med = x.groupby("date")[fw].transform("median")
    x["win"] = (x[fw] > med).astype(float)
    x["tier"] = x.groupby("date")["L"].transform(
        lambda s: pd.qcut(s.rank(method="first"), n_tier, labels=False, duplicates="drop"))
    per = x.groupby(["date", "tier"])["win"].mean().unstack()
    if per.shape[1] < n_tier:
        return None
    spread = (per[n_tier - 1] - per[0]).dropna().iloc[::horizon]
    if len(spread) < validate.MIN_OBS:
        return None
    mu, t, n = validate.newey_west_t(spread.values, lag=1)
    return dict(期數=n, 最高減最低=round(mu * 100, 2), t值=round(t, 2),
                p值=round(validate.bootstrap_p(spread.values), 4),
                正向期比例=round(float((spread > 0).mean()) * 100, 1))


TIER_LABEL = {5: "較佳", 4: "偏佳", 3: "中性", 2: "偏弱", 1: "較弱"}
