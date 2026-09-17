"""L5 驗證層 — 這個系統的計分板。

核心原則：先蓋計分板，再蓋選手。
理由：這類專案的標準失敗模式是「做訊號 -> 回測漂亮 -> 上線 -> 虧錢」，
唯一的防線是在你對任何訊號產生感情之前，就把測量工具蓋好並驗證過。

已處理的統計陷阱：
  * 重疊視窗：h 日前瞻報酬的日序列自相關嚴重 -> Newey-West 修正標準誤。
  * 橫斷面聚類：崩盤日 800 檔同時觸發，那不是 800 個獨立觀察。
    做法是先算每日橫斷面效果，再對「日效果時間序列」檢定（Fama-MacBeth）。
  * 市場 beta：報酬一律橫斷面去均值 -> 量到的是相對表現，不是大盤漲跌。
  * 交易成本：十分位回測扣掉換手成本，未扣成本的數字沒有意義。
"""
import numpy as np
import pandas as pd
from config import COST_ROUND_TRIP


def _rank_pct(s):
    return s.rank(pct=True)


MIN_OBS = 30          # 低於此樣本數一律不報顯著性
MIN_OBS_STRICT = 100  # 低於此標記為「樣本不足，僅供參考」


def newey_west_t(x, lag):
    """對自相關穩健的 t 值。

    小樣本防呆：NW 的自協方差估計在樣本少時很吵，負值加總會把變異數
    壓到比 iid 估計還小，反而放大 t 值 —— 框架自檢就是被這個抓到的。
    因此強制 var >= iid 變異數，NW 只能放大不確定性，不能縮小。
    """
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < MIN_OBS: return (x.mean() if n else np.nan), np.nan, n
    mu = x.mean()
    e = x - mu
    var_iid = (e @ e) / n
    var = var_iid
    for L in range(1, min(lag, n - 1) + 1):
        w = 1.0 - L / (lag + 1.0)
        var += 2.0 * w * (e[L:] @ e[:-L]) / n
    var = max(var, var_iid)              # 只能放大，不能縮小
    if var <= 0: return mu, np.nan, n
    return mu, mu / np.sqrt(var / n), n


def bootstrap_p(x, n_boot=5000, seed=0):
    """移動區塊自助法 p 值 —— 不依賴常態假設，對自相關穩健。"""
    x = np.asarray(x, dtype=float); x = x[~np.isnan(x)]
    n = len(x)
    if n < MIN_OBS: return np.nan
    rng = np.random.default_rng(seed)
    blk = max(1, int(round(n ** (1 / 3))))
    nb = int(np.ceil(n / blk))
    obs = abs(x.mean())
    c = x - x.mean()                      # 在虛無假設（平均為 0）下重抽
    starts = rng.integers(0, n - blk + 1, size=(n_boot, nb))
    idx = (starts[:, :, None] + np.arange(blk)[None, None, :]).reshape(n_boot, -1)[:, :n]
    means = c[idx].mean(axis=1)
    return float((np.abs(means) >= obs).mean())


def daily_ic(panel, factor, horizon=10, neutralize=True, non_overlapping=False):
    """每日橫斷面 Spearman IC。

    non_overlapping=True 時只取每 horizon 天一個橫斷面 —— 從源頭消除重疊，
    使普通 t 檢定成立。這是主要的檢定依據；重疊版本僅供對照。
    """
    fw = f"fwd{horizon}"
    d = panel[["date", factor, fw]].dropna()
    if neutralize:
        d = d.copy()
        d[fw] = d[fw] - d.groupby("date")[fw].transform("mean")   # 市場中性
    def _ic(g):
        if g[factor].nunique() < 2 or len(g) < 50: return np.nan
        return g[factor].rank().corr(g[fw].rank())
    ic = d.groupby("date").apply(_ic, include_groups=False).dropna()
    if non_overlapping and len(ic):
        ic = ic.iloc[::horizon]
    return ic


def ic_report(panel, factors, horizon=10):
    rows = []
    for f in factors:
        ic_all = daily_ic(panel, f, horizon)                      # 重疊，僅供對照
        ic_ind = daily_ic(panel, f, horizon, non_overlapping=True)  # 獨立，檢定依據
        if len(ic_ind) < MIN_OBS:
            rows.append(dict(因子=f, 獨立期數=len(ic_ind), 判定="樣本不足")); continue
        mu = ic_ind.mean(); sd = ic_ind.std()
        t_ind = mu / (sd / np.sqrt(len(ic_ind))) if sd else np.nan
        _, t_naive, _ = (ic_all.mean(),
                         ic_all.mean() / (ic_all.std() / np.sqrt(len(ic_all))) if ic_all.std() else np.nan,
                         len(ic_all))
        p_boot = bootstrap_p(ic_ind.values)
        rows.append(dict(
            因子=f,
            重疊日數=len(ic_all), 獨立期數=len(ic_ind),
            平均IC=round(mu, 4),
            IC_IR=round(mu / sd, 3) if sd else np.nan,
            t值_天真重疊=round(t_naive, 2),
            t值_獨立=round(t_ind, 2),
            p值_自助法=round(p_boot, 4) if p_boot == p_boot else np.nan,
            勝率=round((ic_ind > 0).mean() * 100, 1),
            判定=("顯著" if p_boot == p_boot and p_boot < 0.05 else "不顯著")
                 + ("" if len(ic_ind) >= MIN_OBS_STRICT else " (樣本不足,僅參考)"),
        ))
    return pd.DataFrame(rows)


def decile_backtest(panel, factor, horizon=10, n_dec=10, cost=COST_ROUND_TRIP):
    """十分位多空回測。每 horizon 日換倉一次（不重疊），扣交易成本。"""
    fw = f"fwd{horizon}"
    d = panel[["date", "code", factor, fw]].dropna().copy()
    d[fw] = d[fw] - d.groupby("date")[fw].transform("mean")
    dates = sorted(d["date"].unique())[::horizon]      # 不重疊換倉
    d = d[d["date"].isin(dates)]
    if not len(d): return None
    d["dec"] = d.groupby("date")[factor].transform(
        lambda s: pd.qcut(s.rank(method="first"), n_dec, labels=False, duplicates="drop"))
    g = d.groupby(["date", "dec"])[fw].mean().unstack()
    if g.shape[1] < n_dec: return None
    top, bot = g[n_dec - 1], g[0]
    spread = (top - bot)

    # 換手率用實測，不用假設。相鄰兩期名單會部分重疊，假設 100% 會高估成本。
    # 實測台股這組因子在 10-60 日換倉下約 80-92%。
    ds = sorted(d["date"].unique())
    hold = {dt_: (set(gg.loc[gg["dec"] == n_dec - 1, "code"]),
                  set(gg.loc[gg["dec"] == 0, "code"]))
            for dt_, gg in d.groupby("date")}
    tos = []
    for a, b in zip(ds, ds[1:]):
        for side in (0, 1):
            prev, cur = hold[a][side], hold[b][side]
            if cur:
                tos.append(len(cur - prev) / len(cur))
    turnover = float(np.mean(tos)) if tos else 1.0
    # 成本 = 單邊成本 × 換手率 × 2 條腿
    per_period_cost = cost * 100 * turnover * 2
    net = spread - per_period_cost
    mu, t_nw, n = newey_west_t(net.values, lag=1)
    per_year = 252 / horizon
    cum = (1 + net / 100).cumprod()
    dd = (cum / cum.cummax() - 1).min() * 100
    profile = {int(c): round(float(g[c].mean()), 3) for c in g.columns}
    mono = float(pd.Series(list(profile.values())).corr(
        pd.Series(range(len(profile))))) if len(profile) > 2 else float("nan")
    return dict(
        因子=factor, 期數=n,
        單期毛價差=round(spread.mean(), 3),
        單期淨價差=round(mu, 3),
        換手率=round(turnover * 100, 1),
        單調性=round(mono, 2),
        十分位剖面=profile,
        年化淨報酬=round(mu * per_year, 2),
        年化波動=round(net.std() * np.sqrt(per_year), 2),
        Sharpe=round(mu * per_year / (net.std() * np.sqrt(per_year)), 2) if net.std() else np.nan,
        t值_NW=round(t_nw, 2),
        最大回撤=round(dd, 1),
        勝率=round((net > 0).mean() * 100, 1),
    )


def framework_selftest(panel, horizon=10, n_trials=12, seed=0):
    """框架自檢：餵隨機訊號進去。

    如果隨機雜訊被回報成顯著（|t| > 2），代表框架壞了，後面所有數字都不能信。
    這個測試花 10 分鐘，能擋掉九成的自我欺騙。
    """
    rng = np.random.default_rng(seed)
    p = panel.copy()
    ts, ics, ps = [], [], []
    for i in range(n_trials):
        p["_rand"] = rng.standard_normal(len(p))
        ic = daily_ic(p, "_rand", horizon, non_overlapping=True)
        if len(ic) < MIN_OBS: continue
        mu = ic.mean(); sd = ic.std()
        if not sd: continue
        ts.append(mu / (sd / np.sqrt(len(ic)))); ics.append(mu)
        ps.append(bootstrap_p(ic.values, n_boot=2000, seed=i))
    ts = np.array(ts); ps = np.array(ps)
    if len(ts) < max(5, n_trials // 2):
        return dict(試驗數=len(ts), 判定="無法執行：獨立期數不足，需要更長的歷史資料",
                    說明=f"horizon={horizon} 的非重疊抽樣至少需要 {MIN_OBS} 個獨立期間")
    false_t = float((np.abs(ts) > 2).mean())
    false_p = float((ps < 0.05).mean())
    ok = false_t <= 0.15 and false_p <= 0.15
    return dict(
        試驗數=len(ts),
        平均IC=round(float(np.mean(ics)), 5),
        t值範圍=f"{ts.min():+.2f} ~ {ts.max():+.2f}",
        誤報率_t檢定=f"{false_t*100:.1f}% (期望 ~5%)",
        誤報率_自助法=f"{false_p*100:.1f}% (期望 ~5%)",
        判定="通過：隨機訊號未被誤判為顯著，框架可信" if ok
             else "失敗：框架會把雜訊報成顯著，修好之前不要相信任何結果",
    )


# ------------------------------------------------------------------ 樣本外驗證
def walk_forward(panel, factor_names, horizon=20, train=40, method="ic_weight",
                 cost=COST_ROUND_TRIP, n_dec=5):
    """Walk-forward 樣本外驗證。

    每個換倉時點 T：
      1. 只用 T 之前的資料估計每個因子的滾動 IC（train 期）。
      2. 用那些 IC 當權重合成分數 —— 權重完全不含 T 之後的資訊。
      3. 在 T -> T+horizon 評估。

    這是回答「重選因子」唯一不作弊的方式。直接看全樣本結果再挑因子或翻符號，
    等於用答案反推題目；這裡每個決策點都只看得到當下能看到的東西。

    method:
      "ic_weight" — 以滾動 IC 為權重（IC 為負的因子自然獲得負權重）
      "equal"     — 等權，方向固定不變（對照組）
    """
    fw = "fwd{}".format(horizon)
    cols = ["date", "code", fw] + list(factor_names)
    d = panel[cols].dropna(subset=[fw]).copy()
    d[fw] = d[fw] - d.groupby("date")[fw].transform("mean")

    dates = sorted(d["date"].unique())[::horizon]
    d = d[d["date"].isin(dates)].copy()

    # 每期、每因子的橫斷面 IC（事後才會被當成下一期的權重）
    ic_hist = {}
    for f in factor_names:
        s = d.groupby("date").apply(
            lambda g, f=f: (g[f].rank().corr(g[fw].rank())
                            if g[f].notna().sum() > 50 and g[f].nunique() > 2 else np.nan),
            include_groups=False)
        ic_hist[f] = s
    ic_df = pd.DataFrame(ic_hist).reindex(dates)

    recs = []
    prev_top, prev_bot = set(), set()
    for i, dt_ in enumerate(dates):
        if i < train:
            continue
        hist = ic_df.iloc[i - train:i]          # 嚴格只用 T 之前
        if method == "ic_weight":
            w = hist.mean()
            if not np.isfinite(w).any() or w.abs().sum() == 0:
                continue
            w = w / w.abs().sum()
        else:
            w = pd.Series(1.0 / len(factor_names), index=list(factor_names))

        day = d[d["date"] == dt_]
        if len(day) < 60:
            continue
        z = pd.DataFrame({f: factors_zscore(day[f]) for f in factor_names})
        score = (z * w).sum(axis=1, min_count=1)
        day = day.assign(_s=score).dropna(subset=["_s"])
        if day["_s"].nunique() < n_dec:
            continue
        q = pd.qcut(day["_s"].rank(method="first"), n_dec, labels=False, duplicates="drop")
        top = day[q == n_dec - 1]
        bot = day[q == 0]
        spread = top[fw].mean() - bot[fw].mean()
        tset, bset = set(top["code"]), set(bot["code"])
        to = 1.0
        if prev_top:
            to = (len(tset - prev_top) / max(len(tset), 1)
                  + len(bset - prev_bot) / max(len(bset), 1)) / 2
        prev_top, prev_bot = tset, bset
        recs.append(dict(date=dt_, spread=spread,
                         net=spread - cost * 100 * to * 2, turnover=to))

    if len(recs) < MIN_OBS:
        return dict(方法=method, 期數=len(recs), 判定="樣本不足，無法下結論")
    r = pd.DataFrame(recs)
    mu, t_nw, n = newey_west_t(r["net"].values, lag=1)
    per_year = 252 / horizon
    cum = (1 + r["net"] / 100).cumprod()
    dd = float((cum / cum.cummax() - 1).min() * 100)
    return dict(
        方法=method, 期數=n,
        單期毛價差=round(float(r["spread"].mean()), 3),
        單期淨價差=round(mu, 3),
        換手率=round(float(r["turnover"].mean()) * 100, 1),
        年化淨報酬=round(mu * per_year, 2),
        Sharpe=round(mu * per_year / (r["net"].std() * np.sqrt(per_year)), 2)
        if r["net"].std() else np.nan,
        t值_NW=round(t_nw, 2),
        最大回撤=round(dd, 1),
        勝率=round(float((r["net"] > 0).mean()) * 100, 1),
        p值_自助法=round(bootstrap_p(r["net"].values), 4),
    )


def factors_zscore(s, clip=3.0):
    """單一橫斷面的穩健標準化（中位數 / MAD）。"""
    med = s.median()
    mad = (s - med).abs().median()
    return np.clip((s - med) / (mad * 1.4826 + 1e-12), -clip, clip)
