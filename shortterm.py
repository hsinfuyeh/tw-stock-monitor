"""短線盤後選股（依 stock-screener-PRD.pdf v1.0）。

四個條件加權評分，不要求全部符合：

    1 月營收強勢    年增率 ≥ 20%；創 12 個月新高再加分          25
    2 法人連續買超  投信或外資連續買超 ≥ 3 個交易日              25
    3 技術面突破    收盤 > 前 20 日最高，且量 ≥ 前 5 日均量 × 1.5  25
    4 均線多頭排列  收盤 > MA5 > MA10 > MA20                    25

ETF 沒有月營收，只用 2-4 評分再換算成 100 分，和個股同一張排行。
流動性門檻不計分，未達直接排除：20 日均量 ≥ 1,000 張。

PRD 沒寫死、這裡做的決定（都可在 PARAMS 調整或在下方註明）：

  * 「創 12 個月新高再加分」：條件 1 的權重拆成 80% 給年增率達標、
    20% 給同時創新高。加分只在年增率達標時才算（「再」加分）。
  * 均線與 20 日均量<b>含當日</b>、用原始收盤價 —— 驗收標準要求
    MA20 與看盤軟體一致，看盤軟體就是這樣算的。
  * 「前 20 日最高」與「5 日均量」<b>不含當日</b> —— 否則今天的高點和量
    會算進自己的比較基準，突破與爆量都會被低估。
  * 分數相同時依 20 日成交金額排序（較好買的在前）。
  * 停損價：max(MA20, 進場價 × (1 − 7%))。但若收盤已經跌破 MA20，
    MA20 會高於進場價，那不是停損是停利 —— 這種情況只用 7% 那個。

不構成投資建議，也不會自動下單。
"""
import numpy as np
import pandas as pd

import factors

# 納入的 ETF 次分類：股票型（市值、高股息、主題、海外股票、主動式）。
# 排除槓桿、反向、債券 —— PRD 的明文排除清單。
ETF_OK = ("domestic", "dividend", "active", "foreign")

# (key, 標籤, 預設, 最小, 最大, 步進, 單位, 分組)
PARAMS = [
    ("liq_lots",  "近 20 日平均每天至少",  1000, 0,   20000, 100, "張",     "成交量門檻"),
    ("w_rev",     "佔分",                  25,   0,   100,   5,   "分",     "① 月營收強勢"),
    ("rev_yoy",   "比去年同月至少成長",    20,   0,   300,   5,   "%",      "① 月營收強勢"),
    ("rev_bonus", "創 12 個月新高再加",    20,   0,   100,   5,   "% 的分", "① 月營收強勢"),
    ("w_inst",    "佔分",                  25,   0,   100,   5,   "分",     "② 法人連續買超"),
    ("inst_days", "投信或外資連續買至少",  3,    1,   20,    1,   "天",     "② 法人連續買超"),
    ("w_brk",     "佔分",                  25,   0,   100,   5,   "分",     "③ 突破前 20 日高"),
    ("brk_vol",   "成交量至少是前 5 日平均", 1.5, 1.0, 5.0,   0.1, "倍",     "③ 突破前 20 日高"),
    ("w_ma",      "佔分",                  25,   0,   100,   5,   "分",     "④ 均線多頭排列"),
    ("min_score", "總分至少",              50,   0,   100,   5,   "分",     "清單"),
    ("top_n",     "最多列出",              20,   5,   100,   5,   "檔",     "清單"),
    ("atr_mult",  "停損距離（ATR 的倍數）", 2.0,  0.5, 4.0,   0.5, "倍",     "參考價位"),
    ("hold_days", "最多持有",              20,   3,   30,    1,   "個交易日", "參考價位"),
    ("use_target", "設定目標價（0 關 1 開）", 0,   0,   1,     1,   "",       "參考價位"),
    ("rr",        "目標漲幅是停損的",      2,    0.5, 5,     0.5, "倍",     "參考價位"),
]
STOP_MIN, STOP_MAX = 0.02, 0.10    # 停損距離的上下限（ATR 太小或太大時夾住）
DEFAULTS = {k: d for k, _, d, *_ in PARAMS}
# 不出現在網頁表單、只給回測拆解用：固定百分比停損（0 = 用 ATR）。
# 留著是為了能在同一份回測裡並排比較「舊版固定 −7%」與現在的 ATR 停損。
DEFAULTS["fixed_stop_pct"] = 0
HOLD_MAX = 30                 # 回測預先取的前瞻天數上限，對應 hold_days 的最大值
COST = 0.585                  # PRD：手續費 0.1425% × 2 + 證交稅 0.3%，不計折扣


def params(**kw):
    p = dict(DEFAULTS)
    for k, v in kw.items():
        if k in p and v is not None:
            p[k] = float(v)
    return p


# --------------------------------------------------------------------- 特徵
def _streak(flag, code):
    """每檔股票截至當日「連續為真」的天數。缺值視為中斷。"""
    flag = flag.fillna(False).astype(bool)
    brk = (~flag).groupby(code).cumsum()
    return flag.astype(int).groupby([code, brk]).cumsum()


def features():
    """整個歷史的特徵面板：個股 + 納入的 ETF。算一次，之後調參只重算分數。"""
    parts = [factors.load_panel("common")]
    etf = factors.load_panel("etf", ETF_OK)
    if len(etf):
        parts.append(etf)
    d = pd.concat(parts, ignore_index=True).sort_values(["code", "date"])
    d = d.reset_index(drop=True)
    g = d.groupby("code", sort=False)
    d["kind"] = np.where(d["cat"] == "etf", "ETF", "個股")

    # 均線與 20 日均量：含當日、原始收盤價（對齊看盤軟體）
    for n in (5, 10, 20):
        d["ma{}".format(n)] = g["close"].transform(lambda s: s.rolling(n).mean())
    d["vol20_lots"] = g["volume"].transform(lambda s: s.rolling(20).mean()) / 1000
    d["amt20"] = g["amount"].transform(lambda s: s.rolling(20).mean())
    # ATR14（Wilder）：參考停損的距離用它，不用固定百分比。
    # 除權息日的前一日收盤用參考價，否則配息的價格落差會被算成波動。
    prev = d["ref_price"].where(d["ref_price"].notna(), g["close"].shift(1))
    tr = pd.concat([d["high"] - d["low"], (d["high"] - prev).abs(),
                    (d["low"] - prev).abs()], axis=1).max(axis=1)
    d["atr14"] = tr.groupby(d["code"], sort=False).transform(
        lambda x: x.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())
    d["atrp14"] = d["atr14"] / d["close"]
    # 突破比較基準：不含當日
    d["hi20_prev"] = g["high"].transform(lambda s: s.shift(1).rolling(20).max())
    d["vol5_prev"] = g["volume"].transform(lambda s: s.shift(1).rolling(5).mean())

    # 法人連買：外資、投信分開算
    import store
    inst = store.q("SELECT date, code, foreign_net, trust_net FROM inst")
    inst["date"] = pd.to_datetime(inst["date"]).astype("datetime64[ns]")
    d["date"] = d["date"].astype("datetime64[ns]")
    d = d.merge(inst, on=["date", "code"], how="left")
    d = d.sort_values(["code", "date"]).reset_index(drop=True)
    d["streak_f"] = _streak(d["foreign_net"] > 0, d["code"])
    d["streak_t"] = _streak(d["trust_net"] > 0, d["code"])

    # 月營收（依公布期限對齊，point-in-time）
    import revenue
    rv = revenue.load()
    if len(rv):
        pit = revenue.as_of_panel(rv, d["date"].unique())
        pit["date"] = pit["date"].astype("datetime64[ns]")
        d = d.merge(pit[["date", "code", "yoy", "rev_high12"]],
                    on=["date", "code"], how="left")
    else:
        d["yoy"] = d["rev_high12"] = np.nan
    d = d.sort_values(["code", "date"]).reset_index(drop=True)
    # 除權息調整係數：總報酬指數 / 原始收盤。回測損益用「原始價 × 係數」算，
    # 除息日的價格跳空才不會被當成虧損（配發的現金本來就是持有人的）。
    d["k"] = d["adj"] / d["close"]
    return d


# --------------------------------------------------------------------- 評分
def score(d, p):
    """向量化評分。回傳每列的四個條件是否成立與總分。"""
    liq = d["vol20_lots"] >= p["liq_lots"]
    c1 = d["yoy"] >= p["rev_yoy"]
    hi = c1 & (d["rev_high12"] == 1)
    c2 = (d["streak_f"] >= p["inst_days"]) | (d["streak_t"] >= p["inst_days"])
    c3 = (d["close"] > d["hi20_prev"]) & (d["volume"] >= d["vol5_prev"] * p["brk_vol"])
    c4 = (d["close"] > d["ma5"]) & (d["ma5"] > d["ma10"]) & (d["ma10"] > d["ma20"])

    w1, w2, w3, w4 = p["w_rev"], p["w_inst"], p["w_brk"], p["w_ma"]
    bonus = p["rev_bonus"] / 100.0
    s1 = np.where(c1, w1 * (1 - bonus), 0.0) + np.where(hi, w1 * bonus, 0.0)
    raw = s1 + np.where(c2, w2, 0) + np.where(c3, w3, 0) + np.where(c4, w4, 0)
    full = w1 + w2 + w3 + w4
    is_etf = (d["kind"] == "ETF").to_numpy()
    # ETF 只有 2-4 可以評，換算成同一把尺
    etf_full = w2 + w3 + w4
    sc = np.where(is_etf,
                  (raw - s1) * 100.0 / etf_full if etf_full else 0.0,
                  raw * 100.0 / full if full else 0.0)
    return pd.DataFrame({"liq": liq.to_numpy(), "c1": c1.to_numpy(),
                         "c1hi": hi.to_numpy(), "c2": c2.to_numpy(),
                         "c3": c3.to_numpy(), "c4": c4.to_numpy(),
                         "score": np.round(sc, 1)}, index=d.index)


def pick(d, sc, p):
    """每個交易日的候選：過流動性、分數達門檻、取前 N。"""
    x = d[["date", "code", "amt20"]].join(sc)
    x = x[x["liq"] & (x["score"] >= p["min_score"]) & (x["score"] > 0)]
    x = x.sort_values(["date", "score", "amt20"], ascending=[True, False, False])
    return x.groupby("date", sort=False).head(int(p["top_n"]))


# --------------------------------------------------------------------- 價位與理由
def _tick(p):
    return factors.tick_size(p)


def ref_prices(close, atrp, p):
    """參考價位（依公式計算，不是建議）。

    停損距離 = ATR 的倍數，不是固定百分比 —— 這是回測出來的結果，理由值得寫清楚：

      台股個股的 ATR14 中位數約 2.9%，固定 7% 的停損看起來寬，對波動大的股票
      其實只有兩個 ATR、對波動小的卻有四五個。實測 2019–2026 全部 23 組選股 ×
      7 種停損，<b>停損越緊、扣成本後越差，而且每個策略、每個期間都一樣</b>：
      固定 −3% 時「先碰停損」的比例高達 53–68%，持有中位數只有 1–3 天，
      等於把日常波動變成實現的虧損。改成 2×ATR 之後，這部分的扣分就消失了
      （對 0050 從 −0.34%、t = −2.88 變成 −0.15%、t = −0.60）。詳見 RESEARCH_LOG.md。

    目標價預設<b>不設</b>：設上限會砍掉少數大漲的那幾筆（右尾），
    實測反而讓平均報酬變差。要用的話把 use_target 設成 1。
    """
    close = np.asarray(close, float)
    atrp = np.asarray(atrp, float)
    if p.get("fixed_stop_pct"):
        dist = np.full_like(close, p["fixed_stop_pct"] / 100.0)
    else:
        dist = np.clip(p["atr_mult"] * atrp, STOP_MIN, STOP_MAX)
        dist = np.where(np.isfinite(dist), dist, STOP_MAX)
    stop = close * (1 - dist)
    # 停損往上取整到檔位（虧損不超過算出來的距離），目標往下取整（保守）
    t = _tick(stop)
    stop = np.round(np.ceil(stop / t - 1e-9) * t, 2)
    if not p.get("use_target"):
        return stop, np.full_like(stop, np.nan)
    target = close + p["rr"] * (close - stop)
    t = _tick(target)
    target = np.round(np.floor(target / t + 1e-9) * t, 2)
    return stop, target


def reasons(r, p):
    """逐條列出符合的條件與數值。"""
    out = []
    if r.get("c1") and p["w_rev"] > 0:
        s = "營收年增 {:.0f}%".format(r["yoy"])
        if r.get("c1hi"):
            s += "（創 12 個月新高）"
        out.append(s)
    if r.get("c2") and p["w_inst"] > 0:
        if r["streak_t"] >= p["inst_days"]:
            out.append("投信連買 {} 天".format(int(r["streak_t"])))
        if r["streak_f"] >= p["inst_days"]:
            out.append("外資連買 {} 天".format(int(r["streak_f"])))
    if r.get("c3") and p["w_brk"] > 0:
        out.append("突破前 20 日最高價 {:,.2f}，成交量是前 5 日平均的 {:.1f} 倍".format(
            r["hi20_prev"], r["volume"] / r["vol5_prev"]))
    if r.get("c4") and p["w_ma"] > 0:
        out.append("均線多頭排列（5 日均價 {:,.2f} > 10 日 {:,.2f} > 20 日 {:,.2f}）".format(
            r["ma5"], r["ma10"], r["ma20"]))
    return out


def today(d=None, p=None, date=None):
    """當日候選清單，附理由與參考價位。"""
    p = p or dict(DEFAULTS)
    d = features() if d is None else d
    date = date or d["date"].max()
    day = d[d["date"] == date]
    sc = score(day, p)
    top = pick(day, sc, p)
    rows = day.loc[top.index].join(top[["liq", "c1", "c1hi", "c2", "c3", "c4", "score"]])
    stop, target = ref_prices(rows["close"], rows["atrp14"], p)
    rows = rows.assign(stop=stop, target=target)
    rows["why"] = [reasons(r, p) for _, r in rows.iterrows()]
    return rows


# --------------------------------------------------------------------- 回測
#
# 進場時點：PRD 寫「進場價 = 當日收盤價」，這在畫面上當參考價位沒問題，
# 但回測不能這樣假設 —— 三大法人買賣超 15:00 後才公布、盤後定價交易
# 14:30 就結束，拿到訊號時當天收盤價已經買不到了。所以回測一律
# <b>隔天開盤進場</b>，停損與目標價仍用訊號日算好的那兩個價位。
# 隔天開盤就一字漲停（買不到）的直接略過，不算成交。
#
# 出場（依序檢查，同一天兩者都碰到時算停損 —— 無法得知盤中誰先，取保守）：
#   開盤就跳空在停損之下 -> 以開盤價出場
#   盤中最低 ≤ 停損      -> 以停損價出場
#   開盤就跳空在目標之上 -> 以開盤價出場
#   盤中最高 ≥ 目標      -> 以目標價出場
#   持有滿 hold_days     -> 以當天收盤出場
# 損益含除權息（用總報酬係數 k 還原），扣 PRD 的來回成本 0.585%。
# 同期 0050：同一天開盤買、同一天出場（盤中出場的算到當天收盤），扣 ETF
# 的來回成本 0.385%（證交稅 0.1%），這樣比較才公平。

ETF_COST = 0.1425 * 2 + 0.1


def _forward(d):
    """回測用的連續陣列。d 必須依 code, date 排序。"""
    return {c: d[c].to_numpy() for c in ("open", "high", "low", "close", "k")} | {
        "code": d["code"].to_numpy(), "date": d["date"].to_numpy()}


def _bench(d):
    b = d[d["code"] == "0050"].set_index("date")
    return b["open"] * b["k"], b["close"] * b["k"]


def backtest(d, p, start=None, end=None, arr=None, bench=None):
    """回測。start/end 是訊號日範圍（含）。回傳 (逐筆交易, 摘要)。"""
    p = p or dict(DEFAULTS)
    hold = int(p["hold_days"])
    arr = arr or _forward(d)
    bo, bc = bench if bench is not None else _bench(d)
    m = pd.Series(True, index=d.index)
    if start is not None:
        m &= d["date"] >= start
    if end is not None:
        m &= d["date"] <= end
    sub = d[m]
    top = pick(sub, score(sub, p), p)
    i0 = top.index.to_numpy()
    n = len(arr["code"])
    code = arr["code"]

    # 訊號日的參考價位
    stop, target = ref_prices(d.loc[i0, "close"], d.loc[i0, "atrp14"], p)
    e = i0 + 1
    ok = (e < n)
    ok[ok] = code[e[ok]] == code[i0[ok]]
    # 隔天一字漲停買不到
    ec = np.minimum(e, n - 1)
    locked = (arr["high"][ec] == arr["low"][ec]) & (arr["open"][ec] > d.loc[i0, "close"].to_numpy() * 1.09)
    ok &= ~locked
    entry = arr["open"][ec]
    ok &= np.isfinite(entry) & (entry > 0)

    exit_px = np.full(len(i0), np.nan)
    exit_i = np.full(len(i0), -1)
    why = np.array([""] * len(i0), dtype=object)
    # 目標價可以關掉（預設就是關的）：內部用 +inf 代表永遠碰不到，
    # 但輸出到表格要留原本的缺值 —— inf 不是合法的 JSON，寫出去整頁會載入失敗。
    target_out = target
    target = np.where(np.isfinite(target), target, np.inf)
    hi_seen = np.full(len(i0), -np.inf)
    lo_seen = np.full(len(i0), np.inf)
    open_ = ok.copy()
    for j in range(hold):
        idx = np.minimum(e + j, n - 1)
        alive = open_ & (e + j < n)
        alive[alive] &= code[idx[alive]] == code[i0[alive]]
        # 資料不足以走完持有期 -> 未完成，不計入
        lost = open_ & ~alive
        open_ &= alive
        o, h, lo, c = (arr[x][idx] for x in ("open", "high", "low", "close"))
        kk = arr["k"][idx] / arr["k"][e]          # 除權息還原，跟損益一致
        hi_seen = np.where(open_, np.maximum(hi_seen, h * kk), hi_seen)
        lo_seen = np.where(open_, np.minimum(lo_seen, lo * kk), lo_seen)
        gap_s = open_ & (o <= stop)
        hit_s = open_ & ~gap_s & (lo <= stop)
        gap_t = open_ & ~gap_s & ~hit_s & (o >= target)
        hit_t = open_ & ~gap_s & ~hit_s & ~gap_t & (h >= target)
        last = open_ & ~(gap_s | hit_s | gap_t | hit_t) & (j == hold - 1)
        for mask, px, label in ((gap_s, o, "停損"), (hit_s, stop, "停損"),
                                (gap_t, o, "達標"), (hit_t, target, "達標"),
                                (last, c, "時間")):
            exit_px[mask] = px[mask] if np.ndim(px) else px
            exit_i[mask] = idx[mask]
            why[mask] = label
            open_ &= ~mask
        exit_px[lost] = np.nan
    done = ok & (exit_i >= 0)

    i0, e, x = i0[done], e[done], exit_i[done]
    k_in, k_out = arr["k"][e], arr["k"][x]
    gross = exit_px[done] * k_out / (entry[done] * k_in) - 1
    ret = gross * 100 - COST
    d_in, d_out = arr["date"][e], arr["date"][x]
    mfe = (hi_seen[done] / entry[done] - 1) * 100
    mae = (lo_seen[done] / entry[done] - 1) * 100
    b_in = bo.reindex(pd.DatetimeIndex(d_in)).to_numpy()
    b_out = bc.reindex(pd.DatetimeIndex(d_out)).to_numpy()
    bret = (b_out / b_in - 1) * 100 - ETF_COST
    t = pd.DataFrame({
        "signal": arr["date"][i0], "code": code[i0],
        "name": d.loc[i0, "name"].to_numpy(), "kind": d.loc[i0, "kind"].to_numpy(),
        "score": top.loc[i0, "score"].to_numpy(),
        "sig_close": d.loc[i0, "close"].to_numpy(),
        "stop": stop[done], "target": target_out[done],
        "entry_date": d_in, "entry": entry[done],
        "exit_date": d_out, "exit": exit_px[done], "why": why[done],
        "days": x - e + 1, "ret": ret, "bench": bret, "excess": ret - bret,
        "mfe": mfe, "mae": mae})
    skipped = int((~ok).sum() - (~ok & ~np.isfinite(entry)).sum())
    return t, summarize(t, hold, len(top), skipped)


def summarize(t, hold, n_signal=None, skipped=0):
    if not len(t):
        return {"n": 0, "n_signal": n_signal}
    import validate
    # 每個訊號日的平均超額當一個觀察值。同一天的 20 檔高度相關，
    # 逐筆算 t 值會把樣本數灌大 20 倍；相鄰訊號日的持有期重疊，
    # 所以用 Newey-West、lag = 持有天數。
    daily = t.groupby("signal")["excess"].mean()
    tstat = validate.newey_west_t(daily.to_numpy(), lag=hold)[1]
    days = t["signal"].nunique()
    return {
        "n": len(t), "n_signal": n_signal, "skipped": skipped,
        # 規格書的產品目標：持有期間內曾經漲到 +5%（不是收盤價，是盤中最高）
        "p5": round(float((t["mfe"] >= 5).mean() * 100), 1) if "mfe" in t else None,
        "mfe_med": round(float(t["mfe"].median()), 2) if "mfe" in t else None,
        "mae_med": round(float(t["mae"].median()), 2) if "mae" in t else None,
        "days": days, "start": str(t["signal"].min())[:10], "end": str(t["signal"].max())[:10],
        "win": round(float((t["ret"] > 0).mean() * 100), 1),
        "avg": round(float(t["ret"].mean()), 2),
        "bench": round(float(t["bench"].mean()), 2),
        "excess": round(float(t["excess"].mean()), 2),
        "worst": round(float(t["ret"].min()), 2),
        "t": None if not np.isfinite(tstat) else round(float(tstat), 2),
        "indep": round(days / hold, 1),
        "exits": {k: int(v) for k, v in t["why"].value_counts().items()},
        "hold_avg": round(float(t["days"].mean()), 1),
    }


def windows(d, p, recent=60):
    """PRD 規定的「近 60 個交易日」與整段歷史，同一套規則並排。

    近 60 個交易日取「持有期已經走完」的那 60 天，否則最後幾天的交易
    還沒結束，會被迫提早結算。
    """
    hold = int(p["hold_days"])
    dates = np.sort(d["date"].unique())
    last_ok = dates[-(hold + 2)]
    first_recent = dates[max(0, len(dates) - (hold + 2) - recent + 1)]
    first_all = dates[60]                          # 讓均線與 20 日高有暖身期
    arr, bench = _forward(d), _bench(d)
    out = {}
    for key, s in (("recent", first_recent), ("all", first_all)):
        t, sm = backtest(d, p, s, last_ok, arr, bench)
        out[key] = (t, sm)
    return out


FEATURE_COLS = ["code", "name", "kind", "close", "ma5", "ma10", "ma20",
                "hi20_prev", "volume", "vol5_prev", "vol20_lots", "amt20",
                "streak_f", "streak_t", "yoy", "rev_high12", "atrp14"]

# 拆解測試：每個條件單獨用、以及拿掉停損停利，看虧損是出在選股還是出場規則
ABLATION = [
    ("預設參數", {}),
    ("只用月營收", {"w_inst": 0, "w_brk": 0, "w_ma": 0}),
    ("只用法人連買", {"w_rev": 0, "w_brk": 0, "w_ma": 0}),
    ("只用技術面突破", {"w_rev": 0, "w_inst": 0, "w_ma": 0}),
    ("只用均線多頭", {"w_rev": 0, "w_inst": 0, "w_brk": 0}),
    ("預設，但不設停損", {"atr_mult": 99}),
    ("只用月營收，不設停損", {"w_inst": 0, "w_brk": 0, "w_ma": 0, "atr_mult": 99}),
    ("預設，但停損固定 −7%（舊版）", {"fixed_stop_pct": 7}),
    ("預設，但只持有 10 日", {"hold_days": 10}),
]


def _json_safe(o):
    """遞迴清乾淨：numpy 型別 -> Python，NaN / Infinity -> None。

    NaN 與 Infinity 都不是合法的 JSON。json.dumps 預設會照寫，瀏覽器的 JSON.parse
    直接拋錯 —— 整頁空白，而且產出流程完全不會報錯。2026-09-20 關掉目標價之後
    就是這樣中的（目標價變成 Infinity 寫進逐筆交易表）。所以寫檔前統一過一遍。
    """
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(x) for x in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return float(o) if np.isfinite(o) else None
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (pd.Timestamp,)):
        return str(o)[:10]
    return o if o is None or isinstance(o, (str, int)) else str(o)


def _r(v, nd=2):
    return None if v is None or not np.isfinite(v) else round(float(v), nd)


def track(d, snap):
    """一份快照之後的實際表現。規則和回測完全相同：隔天開盤進場，
    碰到停損或目標出場，最多持有 hold_days 天。還沒走完的標成「持有中」，
    報酬用最新收盤算。一律扣來回成本、含除權息。"""
    date = pd.Timestamp(snap["date"])
    hold = int(snap["params"].get("hold_days", DEFAULTS["hold_days"]))
    codes = [r["code"] for r in snap["top"]] + ["0050"]
    g = d[(d["date"] > date) & d["code"].isin(codes)]
    by = {c: x.reset_index(drop=True) for c, x in g.groupby("code")}
    b = by.get("0050")
    b = b.set_index("date") if b is not None else None
    rows = []
    for r in snap["top"]:
        x = by.get(r["code"])
        row = dict(r, status="等待進場", entry_date=None, entry=None, exit_date=None,
                   exit=None, days=0, ret=None, bench=None)
        if x is None or not len(x):
            rows.append(row)
            continue
        o, h, lo, c, k = (x[col].to_numpy() for col in ("open", "high", "low", "close", "k"))
        stop, target = r["stop"], r["target"]
        e_px = o[0]
        status, xi, px = None, None, None
        for j in range(min(hold, len(x))):
            if o[j] <= stop:
                status, xi, px = "停損出場", j, o[j]
            elif lo[j] <= stop:
                status, xi, px = "停損出場", j, stop
            elif o[j] >= target:
                status, xi, px = "達到目標", j, o[j]
            elif h[j] >= target:
                status, xi, px = "達到目標", j, target
            elif j == hold - 1:
                status, xi, px = "時間到出場", j, c[j]
            if status:
                break
        if status is None:
            status, xi, px = "持有中", len(x) - 1, c[-1]
        ret = (px * k[xi] / (e_px * k[0]) - 1) * 100 - COST
        bret = None
        if b is not None:
            d0, d1 = x["date"][0], x["date"][xi]
            if d0 in b.index and d1 in b.index:
                bret = (b.at[d1, "close"] * b.at[d1, "k"] /
                        (b.at[d0, "open"] * b.at[d0, "k"]) - 1) * 100 - ETF_COST
        row.update(status=status, entry_date=str(x["date"][0])[:10], entry=_r(e_px),
                   exit_date=None if status == "持有中" else str(x["date"][xi])[:10],
                   exit=_r(px), days=int(xi + 1), ret=_r(ret), bench=_r(bret))
        rows.append(row)
    done = [x for x in rows if x["ret"] is not None]
    avg = lambda k: _r(float(np.mean([x[k] for x in done if x[k] is not None]))) if done else None
    return {"date": snap["date"], "n": len(rows),
            "open": sum(x["status"] == "持有中" for x in rows),
            "waiting": sum(x["status"] == "等待進場" for x in rows),
            "avg": avg("ret"), "bench": avg("bench"), "rows": rows}


def compute(d=None, snap_dir=None):
    """算出網頁用的 JSON 與當日快照內容（不寫檔），並做自我檢查。

      short/today.json     當日全部可評分標的的特徵 —— 網頁調參時在瀏覽器裡重算分數，
                           不用重抓資料（PRD：調參只重算分數）
      short/backtest.json  預設參數的回測（近 60 日 + 整段歷史）與拆解測試
      snapshots/<日期>.json 當日清單快照，存進 repo，之後可以對照當天到底選了什麼
    """
    import json
    d = features() if d is None else d
    p = dict(DEFAULTS)
    date = d["date"].max()
    ds = str(date)[:10]
    rows = today(d, p, date)
    day = d[d["date"] == date]

    feat = []
    for r in day[FEATURE_COLS].itertuples(index=False):
        feat.append([r.code, r.name, r.kind] + [_r(getattr(r, c), 4) for c in FEATURE_COLS[3:]])
    top = [{
        "code": r["code"], "name": r["name"], "kind": r["kind"], "score": float(r["score"]),
        "close": float(r["close"]), "stop": float(r["stop"]), "target": float(r["target"]),
        "why": r["why"]} for _, r in rows.iterrows()]
    today_json = {"date": ds, "cols": FEATURE_COLS,
                  "params": [dict(zip(("key", "label", "default", "min", "max", "step",
                                       "unit", "group"), x)) for x in PARAMS],
                  "cost": COST, "rows": feat, "expected": [t["code"] for t in top]}

    # 回測
    w = windows(d, p)
    arr, bench = _forward(d), _bench(d)
    dates = np.sort(d["date"].unique())
    s0, s1 = dates[60], dates[-(int(p["hold_days"]) + 2)]
    abl = []
    for label, kw in ABLATION:
        _, sm = backtest(d, params(**kw), s0, s1, arr, bench)
        abl.append({"label": label, **{k: sm.get(k) for k in
                    ("n", "win", "avg", "bench", "excess", "worst", "t")}})
    ta = w["all"][0]
    yr = ta.groupby(ta["signal"].dt.year).agg(
        n=("ret", "size"), win=("ret", lambda s: (s > 0).mean() * 100),
        avg=("ret", "mean"), bench=("bench", "mean"), excess=("excess", "mean"))
    tr = w["recent"][0]
    trades = [[str(r.signal)[:10], r.code, r.name, r.kind, r.score, r.sig_close, r.stop,
               r.target, str(r.entry_date)[:10], r.entry, str(r.exit_date)[:10],
               r.exit, r.why, int(r.days), _r(r.ret), _r(r.bench), _r(r.excess)]
              for r in tr.sort_values(["signal", "score"], ascending=[False, False])
              .itertuples(index=False)]
    bt = {"date": ds, "cost": COST, "etf_cost": ETF_COST,
          "recent": w["recent"][1], "all": w["all"][1], "ablation": abl,
          "years": [{"year": int(y), **{k: _r(v) for k, v in r.items()}}
                    for y, r in yr.iterrows()],
          "trade_cols": ["選出日", "代號", "名稱", "類型", "分數", "選出日收盤", "停損",
                         "目標", "進場日", "進場價", "出場日", "出場價", "出場原因",
                         "持有天數", "報酬%", "0050%", "贏過 0050%"],
          "trades": trades}

    snap = {"date": ds, "params": p, "top": top}
    check(day, today_json, bt)

    # 歷史清單：每一份過去的快照，之後實際走得怎樣
    snaps = {ds: snap}
    if snap_dir is not None and snap_dir.exists():
        for f in snap_dir.glob("*.json"):
            if f.stem != ds:
                snaps[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    hist = [track(d, snaps[k]) for k in sorted(snaps, reverse=True)]
    return today_json, bt, snap, hist


class Inconsistent(Exception):
    """短線清單的輸入有整欄失效，不該發佈。"""


def check(day, today_json, bt):
    """跟 publish.selfcheck 同一個理由：條件的輸入整欄缺值時，那個條件會
    <b>安靜地</b>變成永遠不成立，清單照樣產出、只是少了一個條件。"""
    bad = []
    stk = day[day["kind"] == "個股"]
    for col, label in (("yoy", "月營收年增率"), ("rev_high12", "營收 12 個月新高"),
                       ("atrp14", "ATR"),
                       ("ma20", "MA20"), ("hi20_prev", "前 20 日最高"),
                       ("vol5_prev", "前 5 日均量"), ("vol20_lots", "20 日均量")):
        if not int(stk[col].notna().sum()):
            bad.append("{}：最新一日整欄缺值".format(label))
    for col, label in (("streak_f", "外資連買"), ("streak_t", "投信連買")):
        if not int((day[col] > 0).sum()):
            bad.append("{}：最新一日沒有任何一檔大於 0（法人資料可能沒進來）".format(label))
    if not today_json["expected"]:
        bad.append("預設參數下清單是空的")
    if not bt["recent"].get("n") or not bt["all"].get("n"):
        bad.append("回測沒有任何交易")
    if bad:
        raise Inconsistent("短線清單未通過自我檢查：\n  - " + "\n  - ".join(bad))
    print("短線自我檢查通過：Top {}、回測 {:,} 筆".format(
        len(today_json["expected"]), bt["all"]["n"]), flush=True)


def write(out, snap_dir, today_json, bt, snap, hist):
    """寫出 JSON 與快照。out 是網站的 data/ 目錄。"""
    import json
    ds = today_json["date"]
    (out / "short").mkdir(parents=True, exist_ok=True)
    for name, obj in (("today.json", today_json), ("backtest.json", bt),
                      ("history.json", hist)):
        # allow_nan=False：NaN / Infinity 都不是合法 JSON，瀏覽器會整頁載入失敗。
        # 寧可在這裡直接爆掉，也不要發佈一個打不開的頁面（2026-09-20 就是這樣中的）。
        (out / "short" / name).write_text(
            json.dumps(_json_safe(obj), ensure_ascii=False, separators=(",", ":"),
                       allow_nan=False),
            encoding="utf-8")

    # 快照：同一天重跑會覆寫（資料若事後修正，快照跟著修正）
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / (ds + ".json")).write_text(json.dumps(
        snap, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    import time
    t0 = time.time()
    d = features()
    print("特徵面板 {:,} 列（{} ~ {}），{:.0f} 秒".format(
        len(d), str(d["date"].min())[:10], str(d["date"].max())[:10], time.time() - t0))
    rows = today(d)
    dt_ = str(d["date"].max())[:10]
    print("\n{} 候選 Top {}（分數 ≥ {}）\n".format(dt_, DEFAULTS["top_n"], DEFAULTS["min_score"]))
    for i, (_, r) in enumerate(rows.iterrows(), 1):
        print("{:>2}. {:<6}{:<8}{:<4}{:>5.0f} 分  收 {:>8,.2f}  停損 {:>8,.2f}  目標 {:>8,.2f}".format(
            i, r["code"], str(r["name"])[:7], r["kind"], r["score"],
            r["close"], r["stop"], r["target"]))
        print("      " + "；".join(r["why"]))
