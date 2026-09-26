"""穩定強勢股：每天 10 檔潛力股名單（RESEARCH_LOG 第 11 輪，事前登記的定義，不要改）。

使用者要的是名單，不是買賣規則：動能強但穩定、避開波動大與疑似被炒作的股票，
看「10 個交易日內能不能漲 5%」。網站不給停利停損、不計成本。

    範圍    上市普通股、20 日成交額中位數前 300、收盤 ≥ 10 元
    排除    波動大、常漲停、常跳空、爆量、60 日已漲 50% 以上、融資暴增、鎖死
            （上線另外排除處置股、近 30 日注意股、全額交割股 —— 這三份沒有歷史，回測不套）
    打分    9 個因子在「通過排除的股票」裡排百分位、等權平均
    名單    前 10 檔，同產業最多 3 檔；不到 10 檔就少給

標籤（評分標準）：隔天開盤為基準，之後 10 個交易日逐日看收盤，
先漲到 +5% 記「中」、先跌到 −5% 記「倒」、都沒有記「平」。
所有價格都用除權息還原，除息日的價格落差不會被當成下跌。

回測只當淘汰關卡：2008–2026 的資料前 10 輪都看過了，乾淨的證據只有上線後的前瞻實測。
"""
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

import barrier
import factors
import validate

ROOT = Path(__file__).parent
SNAP = ROOT / "snapshots" / "stable"

CFG = dict(universe_n=300, min_price=10.0, max_atrp=0.035, max_limitups=1,
           gap=0.04, max_gaps=1, spike=5.0, max_ret60=0.50, max_margin5=0.20,
           margin_min_lots=100, top_n=10, ind_max=3, up=0.05, dn=0.05, hold=10)
LIMIT_CHANGE = pd.Timestamp("2015-06-01")      # 之前漲跌幅 ±7%，之後 ±10%
INST_START = pd.Timestamp("2012-05-02")        # 三大法人資料從這天開始

# (欄位, 名稱, 方向, 白話說明)。方向 -1 代表數字越低越好。
FACTORS = [
    ("f_smooth", "趨勢平滑度", 1, "近 60 天股價走勢有多「穩穩往上」：上漲斜率 × 走勢貼近直線的程度"),
    ("f_eff", "漲得有效率", 1, "近 20 天的漲幅 ÷ 這段期間的上下震盪，用最少的起伏換到最多的漲幅"),
    ("f_updays", "收紅比例", 1, "近 20 天有幾成的日子收漲"),
    ("f_ma", "均線結構", 1, "收盤 > 5 日線 > 10 日線 > 20 日線，而且 20 日線往上，四項成立幾項"),
    ("f_high", "接近一年高點", 1, "股價離近一年最高價多近；越近代表上面套牢的賣壓越少"),
    ("f_rs", "比大盤強", 1, "近 60 天漲幅減掉 0050 的漲幅"),
    ("f_inst", "法人持續買", 1, "近 10 天外資＋投信合計買超的天數比例"),
    ("f_margin", "籌碼乾淨", -1, "融資 20 天增幅減掉股價漲幅；散戶借錢追得比股價漲得快就扣分"),
    ("f_sue", "營收優於預期", 1, "最新月營收比這家公司平常的成長幅度好多少"),
]
EXCLUDE = [
    ("x_atr", "波動太大"), ("x_limit", "常漲停"), ("x_gap", "常跳空"),
    ("x_spike", "曾經爆量"), ("x_ret60", "60 日已漲太多"), ("x_margin", "融資暴增"),
    ("x_locked", "一字漲跌停"),
]
PERIODS = [("2008–2018", "2008-01-01", "2018-12-31"),
           ("2019–2025", "2019-01-01", "2025-12-31"),
           ("2026", "2026-01-01", "2026-12-31")]


# --------------------------------------------------------------------- 特徵
def _rolling_by_code(x, code, n):
    """同一檔股票最近 n 列的和（含當列）。不足 n 列給 NaN。依 code, date 排序。"""
    x = np.asarray(x, dtype=float)
    c = np.concatenate([[0.0], np.nancumsum(np.nan_to_num(x))])
    s = c[n:] - c[:-n]
    out = np.full(len(x), np.nan)
    out[n - 1:] = s
    code = np.asarray(code)
    same = np.zeros(len(x), bool)
    same[n - 1:] = code[n - 1:] == code[:len(x) - n + 1]
    out[~same] = np.nan
    return out


def _smoothness(logp, code, n=60):
    """近 n 日 log 價格對時間回歸的斜率 × R²，向量化。"""
    t = np.arange(len(logp), dtype=float)
    y = np.asarray(logp, float)
    bad = ~np.isfinite(y)
    y0 = np.where(bad, 0.0, y)
    sy, st = _rolling_by_code(y0, code, n), _rolling_by_code(t, code, n)
    sty, stt = _rolling_by_code(t * y0, code, n), _rolling_by_code(t * t, code, n)
    syy = _rolling_by_code(y0 * y0, code, n)
    nb = _rolling_by_code(bad.astype(float), code, n)
    cov = sty / n - (st / n) * (sy / n)
    vt = stt / n - (st / n) ** 2
    vy = syy / n - (sy / n) ** 2
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = cov / vt
        r2 = np.where(vy > 1e-14, cov * cov / (vt * vy), 0.0)
    out = slope * r2
    out[nb > 0] = np.nan
    return out


def limit_up_price(pc, date):
    """漲停價：2015-06-01 前 +7%、之後 +10%，依檔位無條件捨去。"""
    pc = np.asarray(pc, float)
    lim = np.where(np.asarray(date) < LIMIT_CHANGE.to_datetime64(), 1.07, 1.10)
    raw = pc * lim
    t = factors.tick_size(raw)
    with np.errstate(invalid="ignore"):
        return np.floor(raw / t + 1e-9) * t


def limit_dn_price(pc, date):
    pc = np.asarray(pc, float)
    lim = np.where(np.asarray(date) < LIMIT_CHANGE.to_datetime64(), 0.93, 0.90)
    raw = pc * lim
    t = factors.tick_size(raw)
    with np.errstate(invalid="ignore"):
        return np.ceil(raw / t - 1e-9) * t


def features():
    """上市普通股的特徵面板（2008 起）。只用當天收盤已知的資料。"""
    import store
    d = barrier.features(with_revenue=True)
    d = d.sort_values(["code", "date"]).reset_index(drop=True)
    code, date = d["code"].to_numpy(), d["date"].to_numpy()
    g = d.groupby("code", sort=False)

    # 漲跌停與鎖死（依年代切換 7% / 10%）
    up = limit_up_price(d["prev_close"], date)
    dn = limit_dn_price(d["prev_close"], date)
    d["lim_up"] = np.isclose(d["close"], up, rtol=0, atol=1e-6) & d["prev_close"].notna()
    d["lim_dn"] = np.isclose(d["close"], dn, rtol=0, atol=1e-6) & d["prev_close"].notna()
    d["locked"] = (d["high"] == d["low"]) & (d["lim_up"] | d["lim_dn"])
    d["open_locked_up"] = (d["high"] == d["low"]) & (d["open"] >= up - 1e-6) & d["prev_close"].notna()
    d["n_limit20"] = _rolling_by_code(d["lim_up"].astype(float), code, 20)
    d["n_gap20"] = _rolling_by_code((d["gap_pct"].abs() >= CFG["gap"]).astype(float), code, 20)
    spike = (d["rvol20"] >= CFG["spike"]).astype(float)
    d["n_spike20"] = _rolling_by_code(spike, code, 20)

    # 動能與穩定
    d["f_smooth"] = _smoothness(np.log(d["adj"]), code, 60)
    d["f_eff"] = d["ret20"] / (d["rv20"] * np.sqrt(20))
    upday = (d["ret"] > 0).astype(float).where(d["ret"].notna())
    d["f_updays"] = _rolling_by_code(upday.fillna(0), code, 20) / 20
    ma5 = g["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    ma10 = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    ma20 = d["ma20"]
    ma20_5 = ma20.groupby(d["code"], sort=False).shift(5)
    d["ma5"], d["ma10"] = ma5, ma10
    d["f_ma"] = ((d["close"] > ma5).astype(int) + (ma5 > ma10).astype(int)
                 + (ma10 > ma20).astype(int) + (ma20 > ma20_5).astype(int)).astype(float)
    d.loc[ma20_5.isna(), "f_ma"] = np.nan
    d["f_high"] = d["high52_ratio"]
    d["f_rs"] = d["rs_mkt60"]
    d["f_sue"] = d["sue"] if "sue" in d else np.nan

    # 三大法人（外資＋投信）
    inst = store.q("SELECT date, code, foreign_net, trust_net FROM inst")
    inst["date"] = pd.to_datetime(inst["date"]).astype("datetime64[ns]")
    d = d.merge(inst, on=["date", "code"], how="left")
    # 融資餘額（張）。產出時當天的融資券可能還沒公布（約 21:30），就沿用前一天的
    mg = store.q("SELECT date, code, margin_bal FROM margin")
    mg["date"] = pd.to_datetime(mg["date"]).astype("datetime64[ns]")
    d = d.merge(mg, on=["date", "code"], how="left")
    d = d.sort_values(["code", "date"]).reset_index(drop=True)
    code = d["code"].to_numpy()
    last = d["date"].max()
    d["margin_bal"] = d.groupby("code", sort=False)["margin_bal"].transform(
        lambda s: s.ffill(limit=1))
    d.attrs["margin_date"] = str(mg["date"].max())[:10] if len(mg) else None
    d.attrs["margin_same_day"] = bool(len(mg) and mg["date"].max() >= last)

    net = d["foreign_net"].fillna(0) + d["trust_net"].fillna(0)
    has = d["foreign_net"].notna() | d["trust_net"].notna()
    buy = ((net > 0) & has).astype(float)
    d["f_inst"] = _rolling_by_code(buy, code, 10) / 10
    d["inst_days"] = _rolling_by_code(has.astype(float), code, 10)
    d.loc[d["inst_days"] < 10, "f_inst"] = np.nan
    d["inst_buy10"] = d["f_inst"] * 10
    gm = d.groupby("code", sort=False)["margin_bal"]
    m5, m20 = gm.shift(5), gm.shift(20)
    lo = CFG["margin_min_lots"]
    d["margin5"] = (d["margin_bal"] / m5 - 1).where(m5 >= lo)
    d["margin20"] = (d["margin_bal"] / m20 - 1).where(m20 >= lo)
    d["f_margin"] = d["margin20"] - d["ret20"]

    ind = barrier.industry_map()
    d["ind"] = d["code"].map(ind).map(barrier.IND_NAME)
    d["adtv_rank"] = d.groupby("date")["adtv20"].rank(ascending=False, method="first")
    d["hist"] = d.groupby("code", sort=False).cumcount() + 1
    # 只留用得到的欄位、數字轉 float32：全部欄位約 3.8 GB，GitHub 的機器只有 7 GB，
    # 發佈時還同時載著其他面板。標籤要的價格（open/close/k）留 float64，避免 ±5% 邊界被捨入誤差影響。
    attrs = dict(d.attrs)
    d = d[KEEP].copy()
    for c in KEEP:
        if c not in F64 and d[c].dtype == np.float64:
            d[c] = d[c].astype(np.float32)
    for c in ("name", "ind"):
        d[c] = d[c].astype("category")
    d.attrs.update(attrs)
    return d


F64 = ("open", "close", "k", "adtv20")
KEEP = ["date", "code", "name", "ind", "open", "close", "k", "open_locked_up", "locked",
        "adtv20", "adtv_rank", "hist", "ret20", "ret60", "atrp14", "n_limit20", "n_gap20",
        "n_spike20", "margin5", "margin20", "inst_buy10"] + [f for f, *_ in FACTORS]


# --------------------------------------------------------------------- 範圍、排除、打分
def universe(d, cfg=CFG):
    return ((d["adtv_rank"] <= cfg["universe_n"]) & (d["close"] >= cfg["min_price"])
            & (d["hist"] >= 61) & d["ret60"].notna() & d["atrp14"].notna())


def exclusions(d, cfg=CFG):
    """每條排除規則各自的布林欄。缺值一律不排除（資料缺不該讓股票被刷掉）。"""
    return pd.DataFrame({
        "x_atr": d["atrp14"] > cfg["max_atrp"],
        "x_limit": d["n_limit20"] > cfg["max_limitups"],
        "x_gap": d["n_gap20"] > cfg["max_gaps"],
        "x_spike": d["n_spike20"] > 0,
        "x_ret60": d["ret60"] > cfg["max_ret60"],
        "x_margin": d["margin5"] > cfg["max_margin5"],
        "x_locked": d["locked"].fillna(False),
    }, index=d.index).fillna(False)


def pool_mask(d, cfg=CFG):
    return universe(d, cfg) & ~exclusions(d, cfg).any(axis=1)


def score(d, pool):
    """池子裡每個因子排百分位（缺值 0.5），等權平均 -> 0–100 分。"""
    x = d.loc[pool, ["date"] + [f for f, *_ in FACTORS]]
    parts = {}
    for f, _, sign, _ in FACTORS:
        v = x[f] * sign
        r = v.groupby(x["date"]).rank(pct=True)
        parts[f] = r.fillna(0.5)
    p = pd.DataFrame(parts, index=x.index)
    return p, (p.mean(axis=1) * 100).rename("score")


def pick(d, sc, cfg=CFG):
    """依分數取前 N，同產業最多 ind_max 檔。回傳被選中的列索引（依日期、名次）。"""
    x = d.loc[sc.index, ["date", "ind", "adtv20"]].assign(score=sc)
    x = x.sort_values(["date", "score", "adtv20"], ascending=[True, False, False])
    x["_ind"] = x["ind"].astype(object).fillna(pd.Series("_" + x.index.astype(str), index=x.index))
    x["_n"] = x.groupby(["date", "_ind"]).cumcount()
    x = x[x["_n"] < cfg["ind_max"]]
    x["rank"] = x.groupby("date").cumcount() + 1
    x = x[x["rank"] <= cfg["top_n"]]
    return x[["date", "rank", "score"]]


# --------------------------------------------------------------------- 標籤
def labels(d, cfg=CFG):
    """每一列當訊號日的標籤。回傳 DataFrame（同索引）：
      label  1 中、-1 倒、0 平；NaN = 進不了場或窗口還沒走完
      day    分出結果是第幾天
      r10    第 10 個交易日收盤相對基準的還原報酬（%）；停牌或下市為 NaN
      rnow   目前為止最新收盤的報酬（窗口還沒走完的也有，給名單追蹤用）
      nd     已經走了幾個交易日
    """
    n = len(d)
    code = d["code"].to_numpy()
    cal = np.sort(d["date"].unique())
    mi = np.searchsorted(cal, d["date"].to_numpy())
    last_mi = len(cal) - 1
    o, c, k = (d[x].to_numpy(float) for x in ("open", "close", "k"))
    locked_open = d["open_locked_up"].to_numpy()
    hold, up, dn = cfg["hold"], cfg["up"], cfg["dn"]

    i = np.arange(n)
    e = np.minimum(i + 1, n - 1)
    has_e = (i + 1 < n) & (code[e] == code) & (mi[e] == mi + 1)
    # 隔天沒有交易（停牌）、一字漲停買不到：不計
    enter = has_e & ~locked_open[e] & np.isfinite(o[e]) & (o[e] > 0)
    E = o[e] * k[e]
    label = np.full(n, np.nan)
    day = np.full(n, np.nan)
    rnow = np.full(n, np.nan)
    ndone = np.zeros(n, int)
    r10 = np.full(n, np.nan)
    live = enter.copy()
    for j in range(hold):
        idx = np.minimum(e + j, n - 1)
        want = mi + 1 + j                      # 第 j+1 天應該是哪個市場交易日
        future = want > last_mi                # 資料還沒到那天：標籤留 NaN，等之後的資料
        ok = (e + j < n) & (code[idx] == code) & (mi[idx] == want)
        live &= ~future
        gone = live & ~ok                      # 應該有交易卻沒有：停牌或下市，記「倒」
        label[gone], day[gone] = -1, j + 1
        live &= ~gone
        R = c[idx] * k[idx] / E - 1
        rnow = np.where(live, R * 100, rnow)
        ndone = np.where(live, j + 1, ndone)
        hit = live & (R >= up - 1e-12)
        bad = live & ~hit & (R <= -dn + 1e-12)
        label[hit], day[hit] = 1, j + 1
        label[bad], day[bad] = -1, j + 1
        if j == hold - 1:
            r10 = np.where(enter & ok & ~future, R * 100, r10)
            flat = live & ~hit & ~bad
            label[flat], day[flat] = 0, hold
        live &= ~(hit | bad)
    # 分出結果之後 rnow 停在那一天；進行中的 rnow 是最新收盤
    return pd.DataFrame({"label": label, "day": day, "r10": r10,
                         "rnow": rnow, "nd": ndone, "entered": enter}, index=d.index)


# --------------------------------------------------------------------- 回測
def _daily(lab, idx, dates):
    """某一組列（例如名單）每個訊號日的中率、倒率、淨值、第 10 日報酬。"""
    x = lab.loc[idx, ["label", "r10"]].assign(date=dates.loc[idx].to_numpy())
    x = x[x["label"].notna()]
    x["hit"] = (x["label"] == 1).astype(float)
    x["fail"] = (x["label"] == -1).astype(float)
    g = x.groupby("date")
    return pd.DataFrame({"hit": g["hit"].mean(), "fail": g["fail"].mean(),
                         "r10": g["r10"].mean(), "n": g.size()})


def _nw(x, lag):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if len(x) < 30:
        return None
    t = validate.newey_west_t(x, lag)[1]
    return None if not np.isfinite(t) else round(float(t), 2)


def backtest(d, lab, cfg=CFG, start="2008-01-01", end=None):
    """回測：名單與 5 個對照組，逐日配對。回傳 (逐日表, 摘要)。"""
    m = d["date"] >= pd.Timestamp(start)
    if end is not None:
        m &= d["date"] <= pd.Timestamp(end)
    # 只用 10 天窗口已經完全走完的訊號日。最後幾天只有「提早分出結果」的股票有標籤，
    # 留著會只算到已經中或倒的那幾檔（例如某天 1 檔、100% 中），偏差很大。
    cal = np.sort(d["date"].unique())
    m &= d["date"] <= cal[-(cfg["hold"] + 1)]      # 訊號日 + 1 進場 + 10 天 = 最後一天
    uni = universe(d, cfg) & m
    pool = pool_mask(d, cfg) & m
    _, sc = score(d, pool)
    pk = pick(d, sc, cfg)
    dates = d["date"]
    L = lab["label"]
    ok = L.notna()

    top = _daily(lab, pk.index, dates)
    c1 = _daily(lab, pool[pool & ok].index, dates)
    c5 = _daily(lab, uni[uni & ok].index, dates)
    # C2：ATR 五分位配對 —— 每檔名單股換成「同一天、同一個 ATR 五分位」的池平均
    pp = d.loc[pool & ok, ["date", "atrp14"]].copy()
    pr = pp.groupby("date")["atrp14"].rank(pct=True, method="first")
    pp["q"] = np.minimum(np.ceil(pr * 5) - 1, 4).astype(int)
    pp["hit"] = (L[pp.index] == 1).astype(float)
    pp["fail"] = (L[pp.index] == -1).astype(float)
    qm = pp.groupby(["date", "q"])[["hit", "fail"]].mean()
    pq = pp.loc[pp.index.intersection(pk.index), ["date", "q"]]
    mq = qm.reindex(pd.MultiIndex.from_frame(pq)).to_numpy()
    c2 = pd.DataFrame({"date": pq["date"].to_numpy(), "hit": mq[:, 0], "fail": mq[:, 1]}) \
        .groupby("date")[["hit", "fail"]].mean()

    # C3 / C4：池裡 20 日漲幅前 10、ATR 最高 10
    def topby(col):
        x = d.loc[pool, ["date", col]].sort_values(["date", col], ascending=[True, False])
        return _daily(lab, x.groupby("date").head(cfg["top_n"]).index, dates)
    c3, c4 = topby("ret20"), topby("atrp14")

    day = pd.DataFrame({"n": top["n"], "hit": top["hit"], "fail": top["fail"], "r10": top["r10"],
                        "pool_hit": c1["hit"], "pool_fail": c1["fail"], "pool_r10": c1["r10"],
                        "pool_n": c1["n"]})
    day["net"] = day["hit"] - day["fail"]
    for key, c in (("c1", c1), ("c2", c2), ("c3", c3), ("c4", c4), ("c5", c5)):
        day[key] = (c["hit"] - c["fail"]).reindex(day.index)
    day["diff"] = day["net"] - day["c1"]
    day["diff_c2"] = day["net"] - day["c2"]
    day = day.dropna(subset=["net", "c1"])
    return day, summarize(day, cfg)


def _sum(x, hold):
    if not len(x):
        return None
    sub = x.iloc[::hold]
    return {
        "days": int(len(x)), "start": str(x.index.min())[:10], "end": str(x.index.max())[:10],
        "n": int(x["n"].sum()),
        "hit": _p(x["hit"]), "fail": _p(x["fail"]), "net": _p(x["net"]),
        "r10": _r(x["r10"].mean()),
        "pool_hit": _p(x["pool_hit"]), "pool_fail": _p(x["pool_fail"]),
        "pool_r10": _r(x["pool_r10"].mean()), "pool_n": _r(x["pool_n"].mean(), 0),
        **{k: _p(x[k]) for k in ("c1", "c2", "c3", "c4", "c5")},
        "diff": _p(x["diff"]), "t": _nw(x["diff"], hold),
        "diff_c2": _p(x["diff_c2"]), "t_c2": _nw(x["diff_c2"], hold),
        "diff_sub": _p(sub["diff"]), "t_sub": _nw(sub["diff"], 1),
    }


def _p(s):
    v = float(np.nanmean(s)) if len(s) else np.nan
    return None if not np.isfinite(v) else round(v * 100, 1)


def _r(v, nd=2):
    return None if v is None or not np.isfinite(v) else round(float(v), nd)


def summarize(day, cfg=CFG):
    h = cfg["hold"]
    per = {}
    for name, a, b in PERIODS:
        per[name] = _sum(day[(day.index >= a) & (day.index <= b)], h)
    both = day[(day.index >= "2008-01-01") & (day.index <= "2025-12-31")]
    allp = _sum(both, h)
    a, b = per.get("2008–2018"), per.get("2019–2025")
    passed = bool(a and b and allp and a["diff"] > 0 and b["diff"] > 0
                  and (allp["t"] or 0) >= 3 and a["diff_c2"] > 0 and b["diff_c2"] > 0)
    yr = day.groupby(day.index.year)
    years = [{"year": int(y), "days": int(len(g)), "hit": _p(g["hit"]), "fail": _p(g["fail"]),
              "pool_hit": _p(g["pool_hit"]), "pool_fail": _p(g["pool_fail"]),
              "diff": _p(g["diff"])} for y, g in yr]
    return {"periods": per, "all": allp, "passed": passed, "years": years,
            "overall": _sum(day, h)}


def exclusion_stats(d, lab, cfg=CFG, start="2008-01-01"):
    """每條排除規則刷掉多少、被刷掉那批的中率與倒率（照審查意見：先量出代價）。"""
    m = (d["date"] >= pd.Timestamp(start)) & lab["label"].notna()
    uni = universe(d, cfg) & m
    ex = exclusions(d, cfg)[uni]
    L = lab.loc[uni[uni].index, "label"]
    ndays = d.loc[uni, "date"].nunique()
    out = []
    for key, name in EXCLUDE:
        hit = ex[key]
        x = L[hit]
        out.append({"key": key, "name": name,
                    "per_day": _r(hit.sum() / max(ndays, 1), 1),
                    "hit": _p((x == 1).astype(float)) if len(x) else None,
                    "fail": _p((x == -1).astype(float)) if len(x) else None})
    keep = L[~ex.any(axis=1)]
    out.append({"key": "kept", "name": "留下來的（穩定池）",
                "per_day": _r((~ex.any(axis=1)).sum() / max(ndays, 1), 1),
                "hit": _p((keep == 1).astype(float)), "fail": _p((keep == -1).astype(float))})
    out.append({"key": "all", "name": "排除之前（範圍內全部）",
                "per_day": _r(len(L) / max(ndays, 1), 1),
                "hit": _p((L == 1).astype(float)), "fail": _p((L == -1).astype(float))})
    return out


SENSITIVITY = [("範圍前 200", {"universe_n": 200}), ("範圍前 400", {"universe_n": 400}),
               ("ATR 上限 3.0%", {"max_atrp": 0.030}), ("ATR 上限 4.0%", {"max_atrp": 0.040})]


# --------------------------------------------------------------------- 今日名單
def reasons(r, parts):
    """入選原因：分數最高的幾個因子，附上實際數字。"""
    txt = {
        "f_smooth": lambda: "60 日走勢平穩向上（平滑度前 {:.0f}%）".format(
            max(1, round((1 - parts["f_smooth"]) * 100))),
        "f_eff": lambda: "20 日漲 {:.1f}%、震盪小".format(r["ret20"] * 100),
        "f_updays": lambda: "近 20 天有 {:.0f} 天收紅".format(r["f_updays"] * 20),
        "f_ma": lambda: "均線多頭排列" if r["f_ma"] >= 4 else "均線結構 {:.0f}/4".format(r["f_ma"]),
        "f_high": lambda: "距一年高點 {:.1f}%".format((1 - r["f_high"]) * 100)
        if r["f_high"] < 0.999 else "創一年新高",
        "f_rs": lambda: "60 日比大盤多漲 {:.1f}%".format(r["f_rs"] * 100),
        "f_inst": lambda: "法人近 10 天有 {:.0f} 天買超".format(r["inst_buy10"]),
        "f_margin": lambda: "融資沒有追高（20 日融資 {:+.1f}%）".format(r["margin20"] * 100),
        "f_sue": lambda: "月營收優於平常",
    }
    order = sorted(((parts[f], f) for f, *_ in FACTORS
                    if parts.get(f) is not None and np.isfinite(r.get(f, np.nan))), reverse=True)
    out = []
    for v, f in order[:3]:
        if v < 0.6:
            break
        try:
            out.append(txt[f]())
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _risk_codes(risk):
    """處置中、近 30 日注意股、全額交割 -> {代號: [標記]}"""
    flags = {}
    for key, label in (("disposal", "處置股"), ("attention30", "近 30 日注意股"),
                       ("full_delivery", "全額交割")):
        for c in (risk or {}).get(key) or []:
            flags.setdefault(str(c), []).append(label)
    return flags


def today_list(d, date=None, risk=None, cfg=CFG):
    """某一天的名單（含上線才有的處置／注意／全額交割排除）。"""
    date = pd.Timestamp(date or d["date"].max())
    day = d[d["date"] == date]
    pool = pool_mask(day, cfg)
    flags = _risk_codes(risk)
    flagged = day["code"].astype(str).map(lambda c: c in flags)
    pool_live = pool & ~flagged
    parts, sc = score(day, pool_live)
    pk = pick(day, sc, cfg)
    rows = []
    for i, p in pk.iterrows():
        r = day.loc[i]
        pr = parts.loc[i].to_dict()
        rows.append({"rank": int(p["rank"]), "code": r["code"], "name": r["name"],
                     "ind": r["ind"] if isinstance(r["ind"], str) else None,
                     "score": _r(p["score"], 1), "close": _r(r["close"]),
                     "ret20": _r(r["ret20"] * 100, 1), "ret60": _r(r["ret60"] * 100, 1),
                     "atrp": _r(r["atrp14"] * 100, 2), "adtv20": _r(r["adtv20"] / 1e8, 2),
                     "parts": {f: _r(pr[f] * 100, 0) for f, *_ in FACTORS},
                     "why": reasons(r, pr)})
    n_uni = int(universe(day, cfg).sum())
    ex = exclusions(day, cfg)[universe(day, cfg)]
    counts = {k: int(ex[k].sum()) for k, _ in EXCLUDE}
    counts["risk"] = int((universe(day, cfg) & pool & flagged).sum())
    return rows, {"universe": n_uni, "pool": int(pool_live.sum()), "excluded": counts}


def recent_lists(d, n_days=30, cfg=CFG):
    """最近 n 個交易日各自的名單代號（不含上線才有的名單排除）。給連續上榜天數用。"""
    dates = np.sort(d["date"].unique())[-n_days:]
    sub = d[d["date"].isin(dates)]
    pool = pool_mask(sub, cfg)
    _, sc = score(sub, pool)
    pk = pick(sub, sc, cfg)
    codes = sub.loc[pk.index, "code"]
    return {str(dt)[:10]: list(codes[pk["date"] == dt]) for dt in dates}


def streaks(lists, today_codes, today):
    """每檔今天已經連續上榜幾天（含今天）；昨天在榜、今天掉出的代號。"""
    days = sorted(k for k in lists if k < today)
    out = {}
    for c in today_codes:
        n = 1
        for dd in reversed(days):
            if c in lists[dd]:
                n += 1
            else:
                break
        out[c] = n
    prev = lists[days[-1]] if days else []
    dropped = [c for c in prev if c not in today_codes]
    return out, dropped, (days[-1] if days else None)


# --------------------------------------------------------------------- 前瞻實測（名單成績）
def load_snaps():
    out = {}
    if SNAP.exists():
        for f in SNAP.glob("*.json"):
            out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    return out


def persist():
    """快照只由 CI 寫（它會 commit 回 repo）。本機要寫就設 FORWARD_PERSIST=1。"""
    return bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("FORWARD_PERSIST"))


def track(d, lab, snaps, cfg=CFG):
    """每份快照的每一檔：現在的狀態與報酬；每日「名單 − 穩定池」的淨值差。"""
    key = d.set_index(["date", "code"]).index
    pos = pd.Series(np.arange(len(d)), index=key)
    pool = pool_mask(d, cfg)
    L = lab["label"]
    days, rows_out = [], []
    for sd in sorted(snaps):
        s = snaps[sd]
        ts = pd.Timestamp(sd)
        rows = []
        for r in s["rows"]:
            i = pos.get((ts, r["code"]))
            st = {"code": r["code"], "name": r["name"], "rank": r.get("rank")}
            if i is None:
                st.update(status="資料缺", ret=None)
            else:
                lb = lab.iloc[i]
                if not lb["entered"] and d["date"].iloc[i] < d["date"].max():
                    st.update(status="買不到", ret=None)
                elif np.isnan(lb["label"]):
                    st.update(status="進行中" if lb["nd"] else "等開盤", day=int(lb["nd"]),
                              ret=_r(lb["rnow"], 2))
                else:
                    st.update(status={1: "中", -1: "倒", 0: "平"}[int(lb["label"])],
                              day=int(lb["day"]), ret=_r(lb["rnow"], 2))
            rows.append(st)
        done = [x for x in rows if x["status"] in ("中", "倒", "平")]
        pm = pool & (d["date"] == ts) & L.notna()
        pl = L[pm]
        rec = {"date": sd, "n": len(rows), "done": len(done),
               "hit": sum(x["status"] == "中" for x in done),
               "fail": sum(x["status"] == "倒" for x in done),
               "pool_hit": _p((pl == 1).astype(float)) if len(pl) else None,
               "pool_fail": _p((pl == -1).astype(float)) if len(pl) else None,
               "rows": rows}
        if done and len(done) == len([x for x in rows if x["status"] not in ("買不到", "資料缺")]) and len(pl):
            net = (rec["hit"] - rec["fail"]) / len(done)
            rec["diff"] = _r((net - ((pl == 1).mean() - (pl == -1).mean())) * 100, 1)
        days.append(rec)
    finished = [x for x in days if x.get("diff") is not None]
    diffs = np.array([x["diff"] for x in finished], float) / 100
    summ = {"start": min(snaps) if snaps else None, "days": len(days), "finished": len(finished),
            "hit": _r(100 * sum(x["hit"] for x in finished) / max(sum(x["done"] for x in finished), 1), 1)
            if finished else None,
            "fail": _r(100 * sum(x["fail"] for x in finished) / max(sum(x["done"] for x in finished), 1), 1)
            if finished else None,
            "pool_hit": _r(np.mean([x["pool_hit"] for x in finished]), 1) if finished else None,
            "pool_fail": _r(np.mean([x["pool_fail"] for x in finished]), 1) if finished else None,
            "diff": _r(diffs.mean() * 100, 1) if len(diffs) else None,
            "t": _nw(diffs, cfg["hold"]), "target_days": 250, "target_t": 3.0}
    return days[::-1], summ


# --------------------------------------------------------------------- 產出
def compute(d=None, risk=None, with_backtest=True):
    """網頁用的 JSON：today.json、backtest.json、track.json，以及今天要存的快照。"""
    d = features() if d is None else d
    lab = labels(d)
    date = d["date"].max()
    ds = str(date)[:10]
    rows, counts = today_list(d, date, risk)
    lists = recent_lists(d)
    lists[ds] = [r["code"] for r in rows]
    st, dropped, prev_day = streaks(lists, lists[ds], ds)
    names = d[d["date"] == date].set_index("code")["name"].to_dict()
    for r in rows:
        r["streak"] = st.get(r["code"], 1)
    today = {"date": ds, "rows": rows, "counts": counts,
             "dropped": [{"code": c, "name": names.get(c)} for c in dropped], "prev_day": prev_day,
             "margin_date": d.attrs.get("margin_date"), "margin_same_day": d.attrs.get("margin_same_day"),
             "risk": risk, "cfg": CFG,
             "factors": [{"key": f, "name": n, "dir": s, "desc": t} for f, n, s, t in FACTORS],
             "exclude": [{"key": k, "name": n} for k, n in EXCLUDE]}

    bt = None
    if with_backtest:
        day, sm = backtest(d, lab)
        sens = []
        for label, kw in SENSITIVITY:
            _, s2 = backtest(d, lab, dict(CFG, **kw))
            sens.append({"label": label, "all": s2["all"], "periods": s2["periods"]})
        today["baseline"] = {k: sm["all"][k] for k in ("hit", "fail", "pool_hit", "pool_fail")} \
            if sm["all"] else None
        today["passed"] = sm["passed"]
        bt = {"date": ds, "cfg": CFG, **sm, "sensitivity": sens,
              "exclusions": exclusion_stats(d, lab),
              "recent": [{"date": str(i)[:10], **{k: _r(v * 100, 1) if k not in ("n", "pool_n") else int(v)
                                                    for k, v in r.items() if k in
                                                    ("n", "hit", "fail", "pool_hit", "pool_fail", "diff")}}
                         for i, r in day.tail(60).iloc[::-1].iterrows()]}

    snaps = load_snaps()
    snap = {"date": ds, "rows": [{k: r[k] for k in ("rank", "code", "name", "ind", "score", "close", "why")}
                                 for r in rows],
            "margin_same_day": d.attrs.get("margin_same_day"),
            "risk_status": {k: (risk or {}).get(k) for k in ("disposal_status", "notice_status", "full_status")}}
    snaps[ds] = snap
    tdays, tsum = track(d, lab, snaps)
    trk = {"date": ds, "summary": tsum, "days": tdays[:60]}
    check(today, bt)
    return today, bt, trk, snap


class Inconsistent(Exception):
    """名單的輸入有整欄失效，不該發佈。"""


def check(today, bt):
    """跟 shortterm.check 同一個理由：某個因子的輸入整欄缺值時，排序會安靜地少一個因子。"""
    bad = []
    if not today["rows"]:
        bad.append("今天的名單是空的")
    if today["counts"]["pool"] < 30:
        bad.append("穩定池只有 {} 檔，排除規則可能有資料缺值".format(today["counts"]["pool"]))
    parts = [r["parts"] for r in today["rows"]]
    for f, name, *_ in FACTORS:
        if f == "f_sue":
            continue                    # 營收只在公布後一段期間有值，可以整批中間值
        if parts and all(p.get(f) == 50 for p in parts):
            bad.append("{}：今天名單上每一檔都是中間值（資料可能沒進來）".format(name))
    if bt is not None and not (bt.get("all") or {}).get("days"):
        bad.append("回測沒有任何訊號日")
    if bad:
        raise Inconsistent("穩定強勢股名單未通過自我檢查：\n  - " + "\n  - ".join(bad))
    print("名單自我檢查通過：{} 檔、穩定池 {} 檔".format(len(today["rows"]), today["counts"]["pool"]),
          flush=True)


def _json(obj):
    import shortterm
    return json.dumps(shortterm._json_safe(obj), ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def write(out, today, bt, trk, snap):
    (out / "stable").mkdir(parents=True, exist_ok=True)
    for name, obj in (("today.json", today), ("backtest.json", bt), ("track.json", trk)):
        if obj is not None:
            (out / "stable" / name).write_text(_json(obj), encoding="utf-8")
    if persist():
        SNAP.mkdir(parents=True, exist_ok=True)
        import shortterm
        (SNAP / (snap["date"] + ".json")).write_text(
            json.dumps(shortterm._json_safe(snap), ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    import sys
    import time
    sys.stdout.reconfigure(encoding="utf-8")
    t0 = time.time()
    d = features()
    print("特徵面板 {:,} 列，{:.0f} 秒".format(len(d), time.time() - t0), flush=True)
    today, bt, trk, snap = compute(d, with_backtest="--no-bt" not in sys.argv)
    print("\n{} 名單（穩定池 {} 檔）".format(today["date"], today["counts"]["pool"]))
    for r in today["rows"]:
        print("{:>2}. {} {:<8}{:<8}{:>5.1f} 分  {}".format(r["rank"], r["code"], r["name"],
                                                       r["ind"] or "", r["score"], "；".join(r["why"])))
    if bt:
        print(json.dumps({k: bt[k] for k in ("passed", "all", "periods")}, ensure_ascii=False, indent=1))
        print(json.dumps(bt["exclusions"], ensure_ascii=False, indent=1))
        for s in bt["sensitivity"]:
            print(s["label"], json.dumps(s["all"], ensure_ascii=False))
    print("耗時 {:.0f} 秒".format(time.time() - t0))
