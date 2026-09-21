"""規格書 M1–M4 的研究（RESEARCH_LOG.md 第 1 輪，事前登記的 23 組）。

    python study_spec.py            # 跑全部，結果印在畫面並寫成 reports/spec_study.json

只測事前登記的組合。要加組合，先寫進 RESEARCH_LOG.md。
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import barrier
import validate

K = 10
TEST_YEARS = tuple(range(2011, 2026))   # 資料補到 2008 後擴大（RESEARCH_LOG 第 7 輪）
HOLDOUT = pd.Timestamp("2026-01-01")
TRAIN_YEARS = 3
MIN_TRAIN = 100


# --------------------------------------------------------------------- 範圍與候選
def universe(d):
    return (d["adtv20"] >= 5e7) & (d["close"] >= 10) & d["label"].notna()


def cand_score(d, u):
    """rs_mkt20 與 high52_ratio 在當日範圍內的百分位平均。"""
    x = d[u]
    r1 = x.groupby("date")["rs_mkt20"].rank(pct=True)
    r2 = x.groupby("date")["high52_ratio"].rank(pct=True)
    s = pd.Series(np.nan, index=d.index)
    s[u] = (r1 + r2) / 2
    return s


# --------------------------------------------------------------------- 策略（回傳 mask 與排序鍵）
def strategies(d, u, cs):
    S = {}
    S[("B0", "")] = (u & cs.notna(), cs, False)
    S[("B0r", "")] = (u & cs.notna() & (d["regime"] != "down"), cs, False)
    for v in (1.2, 1.5, 2.0):
        for comp in (True, False):
            m = (u & (cs >= 0.7) & (d["ext_ma20_atr"] <= 3) & (d["breakout_dist"] > 0)
                 & (d["rvol20"] >= v))
            if comp:
                # 收斂看的是突破「之前」的狀態：用前一天的收斂旗標
                prev_comp = d.groupby("code", sort=False)["compressed"].shift(1).fillna(False)
                m &= prev_comp.astype(bool)
            S[("M1", "v={} 收斂={}".format(v, "要" if comp else "不要"))] = (m, cs, False)
    base2 = u & (d["close"] > d["ma60"]) & (d["ma20"] > d["ma60"]) & (d["rs_mkt60"] > 0)
    trig2 = (d["close"] > d["prev_high"]) & (d["close"] > d["open"])
    for lo_, hi_ in ((-0.08, -0.02), (-0.12, -0.04)):
        for dry in (True, False):
            m = base2 & trig2 & d["pullback_dd"].between(lo_, hi_)
            if dry:
                m &= d["adtv5"] < d["adtv20"]
            S[("M2", "回落 {:.0f}%~{:.0f}% 量縮={}".format(hi_ * 100, lo_ * 100,
                                                     "要" if dry else "不要"))] = (m, cs, False)
    for s_ in (1.0, 1.5, 2.0):
        for t_ in (7, 15):
            m = u & (d["sue"] >= s_) & (d["days_since"] <= t_) & (d["close"] > d["ma20"])
            S[("M3", "sue≥{} 公布≤{}天".format(s_, t_))] = (m, d["sue"], False)
    liq_top = u & (d[u].groupby("date")["adtv20"].rank(pct=True, ascending=False)
                   .reindex(d.index) <= 0.3)
    for z in (-2.0, -2.5):
        for gate in (True, False):
            m = liq_top & (d["ret5_z"] <= z)
            if gate:
                m &= d["regime"] != "down"
            S[("M4", "z≤{} 大盤非down={}".format(z, "要" if gate else "不要"))] = (
                m, d["ret5_z"], True)
    return S


def pick(d, mask, key, asc, k=K):
    x = d.loc[mask, ["date", "label", "net", "mfe", "mae", "day", "regime", "adtv20"]].copy()
    x["_k"] = key[mask]
    x = x.dropna(subset=["_k"]).sort_values(["date", "_k"], ascending=[True, asc])
    return x.groupby("date", sort=False).head(k)


def prd_picks(d, min_atrp=0):
    """PRD 四條件的前 20 名，只保留個股（標籤只有個股）。

    min_atrp 預設 0：第 1–4 輪跑的時候網站還沒有波動門檻，固定住才能重現當時的數字。
    不要改成跟著 shortterm 的預設值走。
    """
    import shortterm
    f = shortterm.features()
    p = shortterm.params(min_atrp=min_atrp)
    top = shortterm.pick(f, shortterm.score(f, p), p)
    t = f.loc[top.index, ["date", "code"]]
    t["date"] = t["date"].astype("datetime64[ns]")
    x = d.merge(t, on=["date", "code"], how="inner")
    cols = ["date", "label", "net", "mfe", "mae", "day", "regime", "adtv20"]
    return x[cols + (["bench"] if "bench" in x.columns else [])]


# --------------------------------------------------------------------- 指標
def metrics(t, base):
    """t：選到的交易；base：每天範圍平均（net_u）。"""
    if not len(t):
        return {"n": 0}
    t = t.merge(base, left_on="date", right_index=True, how="left")
    t["ex"] = t["net"] - t["net_u"]
    daily = t.groupby("date")["ex"].mean()
    pos, neg = t.loc[t["net"] > 0, "net"].sum(), -t.loc[t["net"] < 0, "net"].sum()
    return {
        "n": int(len(t)), "days": int(t["date"].nunique()),
        "p_up": round(float((t["label"] == 1).mean() * 100), 1),
        "p_stop": round(float((t["label"] == -1).mean() * 100), 1),
        "timeout": round(float((t["label"] == 0).mean() * 100), 1),
        "net": round(float(t["net"].mean()), 3),
        "excess": round(float(t["ex"].mean()), 3),
        "pf": round(float(pos / neg), 2) if neg else None,
        "mfe_med": round(float(t["mfe"].median()), 2),
        "mae_med": round(float(t["mae"].median()), 2),
        "hold_med": float(t["day"].median()),
        "t": round(float(validate.newey_west_t(daily.to_numpy(), lag=10)[1]), 2)
        if len(daily) > 30 else None,
    }


def run(d, tag=""):
    u = universe(d)
    cs = cand_score(d, u)
    base = d[u].groupby("date").agg(net_u=("net", "mean"),
                                    p_up_u=("label", lambda s: (s == 1).mean() * 100))
    S = strategies(d, u, cs)
    picks = {k: pick(d, *v) for k, v in S.items()}
    picks[("PRD", "目前網站")] = prd_picks(d)

    yr = lambda t: t["date"].dt.year
    res = {"full": {}, "wf": {}, "holdout": {}, "years": {}, "regime": {}, "liq": {}}
    pre = lambda t: t[t["date"] < HOLDOUT]
    for k, t in picks.items():
        res["full"][" | ".join(k)] = metrics(pre(t), base)

    # walk-forward：每個策略族在每一年用前 3 年挑參數
    fams = sorted({k[0] for k in picks})
    for fam in fams:
        keys = [k for k in picks if k[0] == fam]
        oos, chosen = [], []
        for y in TEST_YEARS:
            best, bv = None, -1e9
            for k in keys:
                t = picks[k]
                tr = t[(yr(t) >= y - TRAIN_YEARS) & (yr(t) < y)]
                if len(tr) < MIN_TRAIN:
                    continue
                v = metrics(tr, base)["excess"]
                if v > bv:
                    best, bv = k, v
            if best is None:
                continue
            chosen.append((y, best[1]))
            t = picks[best]
            oos.append(t[yr(t) == y])
        o = pd.concat(oos) if oos else pd.DataFrame(columns=picks[keys[0]].columns)
        res["wf"][fam] = dict(metrics(o, base), chosen=chosen)
        # holdout：用 2023–2025 挑出的參數，看 2026 一次
        best, bv = None, -1e9
        for k in keys:
            t = picks[k]
            tr = t[(yr(t) >= 2023) & (yr(t) <= 2025)]
            if len(tr) >= MIN_TRAIN:
                v = metrics(tr, base)["excess"]
                if v > bv:
                    best, bv = k, v
        if best is not None:
            t = picks[best]
            res["holdout"][fam] = dict(metrics(t[t["date"] >= HOLDOUT], base), chosen=best[1])
        # 穩健性（整段、該族第一個組合 + walk-forward 選到最多次的組合）
        top = max(set(c for _, c in chosen), key=[c for _, c in chosen].count) if chosen else keys[0][1]
        t = pre(picks[(fam, top)])
        res["years"][fam] = {int(y): metrics(g, base) for y, g in t.groupby(yr(t))}
        res["regime"][fam] = {str(r): metrics(g, base) for r, g in t.groupby("regime")}
        t = t.assign(liqb=pd.qcut(t["adtv20"], 3, labels=["小", "中", "大"]))
        res["liq"][fam] = {str(r): metrics(g, base) for r, g in t.groupby("liqb", observed=True)}
    return res, picks, base


def show(res):
    cols = ["n", "days", "p_up", "p_stop", "timeout", "net", "excess", "pf", "mfe_med",
            "mae_med", "hold_med", "t"]
    print("\n=== 全部 23 組（2026 以前，樣本內，只供參考）===")
    print(pd.DataFrame(res["full"]).T[cols].to_string())
    print("\n=== Walk-forward 樣本外（{}）===".format("、".join(map(str, TEST_YEARS))))
    wf = pd.DataFrame(res["wf"]).T
    print(wf[cols].to_string())
    for f, r in res["wf"].items():
        print("  {} 每年選到：{}".format(f, "；".join("{}:{}".format(y, c) for y, c in r["chosen"])))
    print("\n=== Holdout 2026（只看一次）===")
    print(pd.DataFrame(res["holdout"]).T[cols + ["chosen"]].to_string())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    t0 = time.time()
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if src:
        d = pd.read_pickle(src)
    else:
        d = barrier.features()
        d = d.join(barrier.labels(d))
    res, picks, base = run(d)
    show(res)
    out = Path("reports")
    out.mkdir(exist_ok=True)
    (out / "spec_study.json").write_text(json.dumps(res, ensure_ascii=False, indent=1,
                                                    default=str), encoding="utf-8")
    print("\n{:.0f} 秒".format(time.time() - t0))
