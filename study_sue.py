"""第 10 輪：營收優於預期 × 大型股 × 持有 40 日（RESEARCH_LOG 事前登記）。

    python study.py sue

規則全部寫死在這裡，不跟著網站預設走 —— 以後改了設定，這一輪的數字仍然重現得出來。
面板用 shortterm.features()（跟網站回測同一份），不用 study.py 傳進來的 barrier 面板。
"""
import sys

import numpy as np
import pandas as pd

import factors
import revenue
import shortterm as st
import store
import validate

TOP_N_UNIV = 150        # 範圍：20 日成交額中位數前 150 的上市普通股
YPCT_MIN = 0.40         # 排除 1：殖利率百分位 < 40%
PPCT_MIN = 0.30         # 排除 2：一年價格位置百分位 < 30%
SUE_PCT = 0.80          # 當月 SUE 前 20%
PICKS = 10              # 每月取前 10 檔
HOLD = 40
STOP = 0.15             # 10b 的寬停損
PERIODS = [("2008–2018", "2008-01-01", "2018-12-31"),
           ("2019–2025", "2019-01-01", "2025-12-31"),
           ("2026", "2026-01-01", "2026-12-31")]


def panel():
    d = st.features()
    d = d[d["kind"] == "個股"].sort_values(["code", "date"]).reset_index(drop=True)
    v = store.q("SELECT date, code, div_yield FROM valuation")
    v["date"] = pd.to_datetime(v["date"]).astype("datetime64[ns]")
    d = d.merge(v, on=["date", "code"], how="left")
    rv = revenue.load()
    pit = revenue.as_of_panel(rv, d["date"].unique())
    pit["date"] = pit["date"].astype("datetime64[ns]")
    d = d.merge(pit[["date", "code", "sue", "days_since"]], on=["date", "code"], how="left")
    d = d.sort_values(["code", "date"]).reset_index(drop=True)
    # 跟 server.py 的漏斗同一個算法：不含當日的 252 日高低、當天橫斷面百分位
    g = d.groupby("code", sort=False)
    hi = g["high"].transform(lambda x: x.shift(1).rolling(252, min_periods=60).max())
    lo = g["low"].transform(lambda x: x.shift(1).rolling(252, min_periods=60).min())
    d["pos252"] = (d["close"] - lo) / (hi - lo)
    by = d.groupby("date")
    d["ypct"] = by["div_yield"].rank(pct=True)
    d["ppct"] = by["pos252"].rank(pct=True)
    d["urank"] = by["adtv20"].rank(ascending=False, method="first")
    d["prev_close"] = g["close"].shift(1)
    return d


def signals(d, rng=None, mode="rule"):
    """回傳每月選到的列索引。mode: rule / random（同範圍與排除、不看 SUE）/ sue_only（只要 L1 流動性）"""
    day = d[d["days_since"] == 0]
    day = day.assign(spct=day.groupby("date")["sue"].rank(pct=True))
    univ = (day["urank"] <= TOP_N_UNIV) & ~(day["ypct"] < YPCT_MIN) & ~(day["ppct"] < PPCT_MIN)
    if mode == "random":
        x = day[univ]
        return np.concatenate([rng.choice(g.index.to_numpy(), min(PICKS, len(g)), replace=False)
                               for _, g in x.groupby("date")])
    if mode == "sue_only":
        univ = day["amt20"] >= 2e7
    x = day[univ & (day["spct"] >= SUE_PCT)]
    return x.sort_values(["date", "sue"], ascending=[True, False]).groupby("date").head(PICKS).index.to_numpy()


def trades(d, idx, stop=None):
    a = {c: d[c].to_numpy() for c in ("open", "high", "low", "close", "k", "prev_close")}
    code, date = d["code"].to_numpy(), d["date"].to_numpy()
    bo, bc = _bench()
    n = len(d)
    idx = np.asarray(idx)
    e = idx + 1
    ok = e + HOLD - 1 < n
    ok[ok] = (code[e[ok]] == code[idx[ok]]) & (code[e[ok] + HOLD - 1] == code[idx[ok]])
    ec = np.minimum(e, n - 1)
    ok &= ~((a["high"][ec] == a["low"][ec]) & (a["open"][ec] > a["close"][idx] * 1.05))
    ok &= np.isfinite(a["open"][ec]) & (a["open"][ec] > 0)
    idx, e = idx[ok], e[ok]
    E = a["open"][e] * a["k"][e]
    S = E * (1 - stop) if stop else np.full(len(e), -np.inf)
    px = np.full(len(e), np.nan)
    xi = np.full(len(e), -1)
    live = np.ones(len(e), bool)
    for j in range(HOLD):
        i = e + j
        O, H, L, C = (a[c][i] * a["k"][i] for c in ("open", "high", "low", "close"))
        stuck = (a["high"][i] == a["low"][i]) & (a["close"][i] <= a["prev_close"][i] * 0.905)
        gs = live & ~stuck & (j > 0) & (O <= S)
        hs = live & ~stuck & ~gs & (L <= S)
        to = live & ~gs & ~hs & (j == HOLD - 1)
        for m, p in ((gs, O), (hs, S), (to, C)):
            px[m], xi[m] = p[m], i[m]
            live &= ~m
    # 最後一天還鎖跌停賣不掉的：用那天收盤記（保守處理，極少見）
    left = live
    px[left], xi[left] = (a["close"][e[left] + HOLD - 1] * a["k"][e[left] + HOLD - 1]), e[left] + HOLD - 1
    ret = (px / E - 1) * 100 - st.COST
    b = (bc.reindex(pd.DatetimeIndex(date[xi])).to_numpy()
         / bo.reindex(pd.DatetimeIndex(date[e])).to_numpy() - 1) * 100 - st.ETF_COST
    return pd.DataFrame({"sig": date[idx], "code": code[idx], "ret": ret, "b": b,
                         "ex": ret - b, "stopped": xi < e + HOLD - 1}).dropna()


_B = {}


def _bench():
    """0050 還原開盤／收盤（panel 只留個股，所以另外載）。"""
    if not _B:
        full = factors.load_panel("etf", ("domestic",))
        b = full[full["code"] == "0050"].set_index("date")
        k = b["adj"] / b["close"]
        _B["v"] = (b["open"] * k, b["close"] * k)
    return _B["v"]


def stats(t):
    if len(t) < 10:
        return {"n": len(t)}
    m = t.groupby("sig")["ex"].mean()
    tv = validate.newey_west_t(m.to_numpy(), 2)[1] if len(m) >= 12 else np.nan
    los = t.loc[t["ret"] < 0, "ret"]
    return {"months": len(m), "n": len(t), "vs0050": m.mean(), "t": tv,
            "win": (t["ret"] > 0).mean() * 100, "beat": (t["ex"] > 0).mean() * 100,
            "avgloss": los.mean(), "p10": t["ret"].quantile(0.1), "worst": t["ret"].min(),
            "stopped%": t["stopped"].mean() * 100}


def main():
    d = panel()
    rng = np.random.default_rng(0)
    arms = [("10a 不停損", signals(d), None), ("10b 停損 −15%", signals(d), STOP),
            ("對照：同範圍隨機 10 檔", signals(d, rng, "random"), None),
            ("對照：只看 SUE（僅 L1 流動性）", signals(d, mode="sue_only"), None)]
    rows, verdict = [], {}
    for name, idx, stop in arms:
        t = trades(d, idx, stop)
        for pn, a, b in PERIODS + [("2008–2025", "2008-01-01", "2025-12-31")]:
            rows.append({"arm": name, "period": pn,
                         **stats(t[(t["sig"] >= a) & (t["sig"] <= b)])})
        if name.startswith("10"):
            p = {r["period"]: r for r in rows if r["arm"] == name}
            verdict[name] = (p["2008–2018"].get("vs0050", -1) > 0 and p["2019–2025"].get("vs0050", -1) > 0
                             and (p["2008–2025"].get("t") or 0) >= 2)
    pd.set_option("display.width", 220)
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    for k, v in verdict.items():
        print("{}：{}".format(k, "回測支持" if v else "回測不支持"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
