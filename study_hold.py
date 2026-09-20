"""第 3 輪：目標價與持有期（RESEARCH_LOG.md 2026-09-20）。"""
import sys, time
import numpy as np, pandas as pd
import barrier, study_spec as S

CONFIGS = [("B0r", ""), ("M1", "v=1.2 收斂=要"), ("M3", "sue≥2.0 公布≤7天"),
           ("M4", "z≤-2.5 大盤非down=要"), ("PRD", "目前網站")]
EXITS = [("+5% 上限 / 10 日", dict(up=0.05, dn=0.99, hold=10)),
         ("純持有 10 日", dict(up=9.99, dn=0.99, hold=10)),
         ("純持有 20 日", dict(up=9.99, dn=0.99, hold=20))]
IN_END, OOS_END = pd.Timestamp("2023-01-01"), pd.Timestamp("2026-01-01")

def main(src):
    d0 = pd.read_pickle(src)
    rows, years = [], []
    for name, kw in EXITS:
        d = d0.drop(columns=["label", "day", "net", "mfe", "mae"], errors="ignore")
        d = d.join(barrier.labels(d, **kw))
        u = S.universe(d); cs = S.cand_score(d, u); strat = S.strategies(d, u, cs)
        base = d[u].groupby("date").agg(net_u=("net", "mean"))
        picks = {"範圍基準": d.loc[u, ["date","label","net","mfe","mae","day","regime","adtv20"]]}
        for fam, cfg in CONFIGS:
            picks[fam] = S.prd_picks(d) if fam == "PRD" else S.pick(d, *strat[(fam, cfg)])
        for k, t in picks.items():
            for period, sel in (("樣本內 19-22", t[t["date"] < IN_END]),
                                ("樣本外 23-25", t[(t["date"] >= IN_END) & (t["date"] < OOS_END)]),
                                ("holdout 26", t[t["date"] >= OOS_END])):
                m = S.metrics(sel, base)
                if m.get("n"): rows.append(dict(exit=name, strat=k, period=period, **m))
            if k in ("M3", "M4", "PRD", "範圍基準"):
                for y, g in t.groupby(t["date"].dt.year):
                    m = S.metrics(g, base)
                    if m.get("n"): years.append(dict(exit=name, strat=k, year=int(y), **m))
    r, ry = pd.DataFrame(rows), pd.DataFrame(years)
    for v in ("net", "excess", "p_up", "n", "t"):
        print("\n===== {} =====".format(v))
        print(r.pivot_table(index=["exit","period"], columns="strat", values=v, sort=False).round(2).to_string())
    print("\n===== 逐年 net（純持有 10 日）=====")
    print(ry[ry.exit=="純持有 10 日"].pivot_table(index="year", columns="strat", values="net", sort=False).round(2).to_string())
    print("\n===== 逐年 excess（純持有 10 日）=====")
    print(ry[ry.exit=="純持有 10 日"].pivot_table(index="year", columns="strat", values="excess", sort=False).round(2).to_string())
    r.to_json("reports/hold_study.json", orient="records", force_ascii=False)
    ry.to_json("reports/hold_years.json", orient="records", force_ascii=False)

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8"); t0=time.time(); main(sys.argv[1]); print("{:.0f} 秒".format(time.time()-t0))
