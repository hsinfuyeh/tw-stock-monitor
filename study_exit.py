"""第 2 輪：出場幾何（RESEARCH_LOG.md 2026-09-20）。

策略選股不變（第 1 輪 walk-forward 選到的參數），只換停損設定，
看「停損太緊」是不是負期望值的主因。
"""
import sys
import time

import numpy as np
import pandas as pd

import barrier
import study_spec as S

EXITS = [
    ("固定 -2%", lambda d: 0.02),
    ("固定 -2.5%", lambda d: 0.025),
    ("固定 -3%", lambda d: 0.03),
    ("1.0×ATR", lambda d: np.clip(d["atrp14"].to_numpy(), 0.02, 0.10)),
    ("1.5×ATR", lambda d: np.clip(1.5 * d["atrp14"].to_numpy(), 0.02, 0.10)),
    ("2.0×ATR", lambda d: np.clip(2.0 * d["atrp14"].to_numpy(), 0.02, 0.10)),
    ("不停損", lambda d: 0.99),
]

# 第 1 輪 walk-forward 選到的設定（M3 取 2022–2024 選到的那組）
CONFIGS = [
    ("B0r", ""),
    ("M1", "v=1.2 收斂=要"),
    ("M2", "回落 -2%~-8% 量縮=要"),
    ("M3", "sue≥2.0 公布≤7天"),
    ("M4", "z≤-2.5 大盤非down=要"),
    ("PRD", "目前網站"),
]
IN_END = pd.Timestamp("2019-01-01")      # 樣本內：2008–2018
OOS_END = pd.Timestamp("2026-01-01")     # 樣本外：2019–2025；之後是 holdout


def main(src):
    d0 = pd.read_pickle(src)
    rows = []
    for name, fn in EXITS:
        d = d0.drop(columns=barrier.LABEL_COLS, errors="ignore")
        L = barrier.labels(d, dn=fn(d))
        d = d.join(L)
        u = S.universe(d)
        cs = S.cand_score(d, u)
        strat = S.strategies(d, u, cs)
        base = d[u].groupby("date").agg(net_u=("net", "mean"))
        picks = {"範圍基準": d.loc[u, ["date", "label", "net", "mfe", "mae", "day",
                                       "regime", "adtv20"]]}
        for fam, cfg in CONFIGS:
            if fam == "PRD":
                picks["PRD"] = S.prd_picks(d)
            else:
                picks[fam] = S.pick(d, *strat[(fam, cfg)])
        for k, t in picks.items():
            for period, sel in (("樣本內 08-18", t[t["date"] < IN_END]),
                                ("樣本外 19-25", t[(t["date"] >= IN_END) & (t["date"] < OOS_END)]),
                                ("holdout 26", t[t["date"] >= OOS_END])):
                m = S.metrics(sel, base)
                if m.get("n"):
                    rows.append(dict(exit=name, strat=k, period=period, **m))
    r = pd.DataFrame(rows)
    cols = ["n", "p_up", "p_stop", "timeout", "net", "excess", "pf", "hold_med", "t"]
    for period in ("樣本內 08-18", "樣本外 19-25", "holdout 26"):
        print("\n=== {} ===".format(period))
        x = r[r["period"] == period]
        print(x.pivot_table(index="exit", columns="strat", values="net", sort=False).round(2).to_string())
        print("-- 相對同日範圍平均的超額 --")
        print(x.pivot_table(index="exit", columns="strat", values="excess", sort=False).round(2).to_string())
        print("-- +5% 先到 % --")
        print(x.pivot_table(index="exit", columns="strat", values="p_up", sort=False).round(1).to_string())
    r.to_json("reports/exit_study.json", orient="records", force_ascii=False)
    print("\n完整表格 reports/exit_study.json")
    best = r[(r["period"] == "樣本內 08-18")].sort_values("net", ascending=False).head(8)
    print("\n樣本內最好的 8 組：")
    print(best[["exit", "strat"] + cols].to_string(index=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    t0 = time.time()
    main(sys.argv[1])
    print("{:.0f} 秒".format(time.time() - t0))
