"""排除法（avoidance screening）的系統性測試。

核心假設（使用者提出）：
    「算出哪些不能買」比「算出哪些可以買」容易。

為什麼這個想法有道理 —— 成本結構的不對稱：
    選擇：訊號必須大於 0.6% 的來回成本，因為你要為此下單。
    排除：門檻是 0，因為不買某檔不用付任何錢。

但要注意這降低的是「經濟門檻」，不是「證據門檻」。
排除錯的東西一樣會虧 —— 實測「排除高波動股」反而虧 1.92%/年。

測試設計上刻意避開前面示範過的參數探勘陷阱：
  * 所有規則事先寫死在 RULES，不邊看結果邊調。
  * 全部結果一起報告，不只報最好的。
  * 有 walk-forward 版本，不只看樣本內。
  * 多重檢定校正。
"""
import numpy as np
import pandas as pd

import validate

# ---------------------------------------------------------------------------
# 事先寫死的排除規則。每條都要有「為什麼該排除」的先驗理由，
# 不是先看資料再回頭編理由。
RULES = {
    "流動性最低20%": dict(
        col="liqpct", op="<", th=0.20,
        why="買不到也賣不掉。這是執行面事實，不是預測"),
    "流動性最低40%": dict(
        col="liqpct", op="<", th=0.40,
        why="同上，更嚴格"),
    "波動最高20%": dict(
        col="sdpct", op=">", th=0.80,
        why="風險大；但要注意高風險未必等於低報酬"),
    "波動最低20%": dict(
        col="sdpct", op="<", th=0.20,
        why="對照組：如果排高波動有效，排低波動就該無效"),
    "綜合分數最高30%": dict(
        col="spct", op=">", th=0.70,
        why="十分位剖面顯示分數越高後續越差（單調性 -0.61）"),
    "綜合分數最高50%": dict(
        col="spct", op=">", th=0.50,
        why="同上，更嚴格"),
    "近20日曾漲跌停": dict(
        col="limitflag", op=">", th=0.5,
        why="曾鎖死代表流動性風險，掛市價單危險"),
    "法人賣超最多20%": dict(
        col="instpct", op="<", th=0.20,
        why="法人買超是唯一通過驗證的訊號，其反面應為最差"),
    "價格位置最高10%": dict(
        col="pospct", op=">", th=0.90,
        why="接近一年高點，回檔空間大"),
    "價格位置最低10%": dict(
        col="pospct", op="<", th=0.10,
        why="對照組：接近低點，下跌通常有原因"),
}


def prepare(panel, horizon=10):
    """加上排除規則需要的各種橫斷面百分位。"""
    d = panel.dropna(subset=["fwd{}".format(horizon)]).copy()
    fw = "fwd{}".format(horizon)
    d["ex"] = d[fw] - d.groupby("date")[fw].transform("mean")
    g = d.groupby("date")
    d["liqpct"] = g["amt20"].rank(pct=True)
    d["sdpct"] = g["sd60"].rank(pct=True)
    if "S" in d:
        d["spct"] = g["S"].rank(pct=True)
    if "法人買超" in d:
        d["instpct"] = g["法人買超"].rank(pct=True)
    d["limitflag"] = d["limit_20d"].fillna(0)
    if "pos252" in d:
        d["pospct"] = g["pos252"].rank(pct=True)
    return d


def _mask(d, rule):
    """回傳「要保留」的布林遮罩（也就是 NOT 被排除）。"""
    col, op, th = rule["col"], rule["op"], rule["th"]
    if col not in d.columns:
        return None
    v = d[col]
    hit = (v > th) if op == ">" else (v < th)
    return ~hit.fillna(False)


def test_pool(d, keep, horizon=10, label=""):
    """測「剩餘池子」的平均超額報酬。"""
    sub = d[keep]
    if not len(sub):
        return None
    per = sub.groupby("date")["ex"].mean().iloc[::horizon]
    if len(per) < validate.MIN_OBS:
        return None
    mu, t, n = validate.newey_west_t(per.values, lag=1)
    return dict(規則=label, 排除比例=round((1 - keep.mean()) * 100, 1),
                期數=n, 單期改善=round(mu, 4),
                年化=round(mu * 252 / horizon, 2),
                t值=round(t, 2),
                p值=round(validate.bootstrap_p(per.values), 4),
                有效期比例=round(float((per > 0).mean()) * 100, 1))


def test_workflow(d, screen_col, top_n=20, keep=None, horizon=10, label=""):
    """測真實流程：先用某個榜單取前 N 檔，再套排除規則。

    這才是實際會發生的事 —— 沒有人會對全市場 1,000 檔套規則，
    而是先篩出候選再過濾。池子變小之後排除的效益與代價都會改變。
    """
    x = d if keep is None else d[keep]
    recs = []
    for dt_, day in x.groupby("date"):
        if len(day) < top_n:
            continue
        pick = day.nlargest(top_n, screen_col)
        recs.append((dt_, pick["ex"].mean(), len(pick)))
    if len(recs) < validate.MIN_OBS * horizon:
        return None
    r = pd.DataFrame(recs, columns=["date", "ex", "n"]).iloc[::horizon]
    if len(r) < validate.MIN_OBS:
        return None
    mu, t, n = validate.newey_west_t(r["ex"].values, lag=1)
    return dict(流程=label, 期數=n, 平均持股=round(float(r["n"].mean()), 1),
                單期超額=round(mu, 4), 年化=round(mu * 252 / horizon, 2),
                t值=round(t, 2),
                p值=round(validate.bootstrap_p(r["ex"].values), 4),
                有效期比例=round(float((r["ex"] > 0).mean()) * 100, 1))


def stability(d, rule_keep, horizon=10, n_split=4):
    """穩定性：把樣本切成 N 段，看每段的方向是否一致。

    排除法若真的比選擇法「容易」，應該在分段上更穩定，
    而不只是全樣本平均比較好看。
    """
    sub = d[rule_keep]
    per = sub.groupby("date")["ex"].mean().iloc[::horizon]
    if len(per) < n_split * 8:
        return None
    chunks = np.array_split(per.values, n_split)
    means = [float(c.mean()) for c in chunks]
    same = sum(1 for m in means if m > 0)
    return dict(各段=[round(m * 252 / horizon, 2) for m in means],
                同向段數="{}/{}".format(max(same, n_split - same), n_split))
