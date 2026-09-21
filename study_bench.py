"""第 4 輪：跟「改買 0050」比（RESEARCH_LOG.md 2026-09-20）。

第 3 輪的「超額」是跟同一天範圍內所有股票的平均比 —— 那衡量的是選股能力。
但使用者真正的替代方案是直接買 0050，所以這裡每一筆都跟同期間的 0050 比。
"""
import sys, time
import numpy as np, pandas as pd
import barrier, validate, study_spec as S

CONFIGS = [("B0r", ""), ("M1", "v=1.2 收斂=要"), ("M3", "sue≥2.0 公布≤7天"),
           ("M4", "z≤-2.5 大盤非down=要"), ("PRD", "目前網站")]
EXITS = [("+5% 上限 / 10 日", dict(up=0.05, dn=0.99, hold=10)),
         ("純持有 10 日", dict(up=9.99, dn=0.99, hold=10)),
         ("純持有 20 日", dict(up=9.99, dn=0.99, hold=20)),
         ("2×ATR 停損 / 20 日", dict(up=9.99, dn=None, hold=20))]
IN_END, OOS_END = pd.Timestamp("2019-01-01"), pd.Timestamp("2026-01-01")

def stats(t, hold):
    if not len(t): return {}
    ex = t["net"] - t["bench"]
    daily = t.groupby("date").apply(lambda g: (g["net"] - g["bench"]).mean())
    return {"n": len(t), "net": round(t["net"].mean(), 2), "b0050": round(t["bench"].mean(), 2),
            "vs0050": round(ex.mean(), 2), "win": round((t["net"] > 0).mean() * 100, 1),
            "beat": round((ex > 0).mean() * 100, 1),
            "t": round(float(validate.newey_west_t(daily.to_numpy(), lag=hold)[1]), 2) if len(daily) > 30 else None}

def main(src):
    d0 = pd.read_pickle(src)
    m = barrier.market()
    rows = []
    for name, kw in EXITS:
        d = d0.drop(columns=barrier.LABEL_COLS, errors="ignore")
        kw = dict(kw)
        if kw["dn"] is None: kw["dn"] = np.clip(2.0 * d["atrp14"].to_numpy(), 0.02, 0.10)
        lab = barrier.labels(d, **kw)
        d = d.join(lab).assign(bench=barrier.bench_returns(d, lab, m))
        u = S.universe(d); cs = S.cand_score(d, u); strat = S.strategies(d, u, cs)
        cols = ["date","label","net","mfe","mae","day","regime","adtv20","bench"]
        picks = {"範圍基準": d.loc[u, cols]}
        for fam, cfg in CONFIGS:
            picks[fam] = (S.prd_picks(d.assign(**{})) if fam == "PRD" else S.pick(d, *strat[(fam,cfg)]))
            if fam == "PRD":
                picks[fam] = d.merge(picks[fam][["date"]].drop_duplicates(), on="date", how="inner") if False else picks[fam]
        for k, t in picks.items():
            if "bench" not in t.columns:
                t = t.join(d["bench"])
            for period, sel in (("樣本內 08-18", t[t["date"] < IN_END]),
                                ("樣本外 19-25", t[(t["date"]>=IN_END)&(t["date"]<OOS_END)]),
                                ("holdout 26", t[t["date"] >= OOS_END])):
                r = stats(sel.dropna(subset=["bench"]), kw["hold"])
                if r: rows.append(dict(exit=name, strat=k, period=period, **r))
    r = pd.DataFrame(rows)
    for v in ("net", "b0050", "vs0050", "beat", "t"):
        print("\n===== {} =====".format(v))
        print(r.pivot_table(index=["exit","period"], columns="strat", values=v, sort=False).round(2).to_string())
    r.to_json("reports/bench_study.json", orient="records", force_ascii=False)

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8"); t0=time.time(); main(sys.argv[1]); print("{:.0f} 秒".format(time.time()-t0))
