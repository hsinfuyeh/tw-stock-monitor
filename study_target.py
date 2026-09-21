"""第 5 輪：淨賺 5% 的目標價（RESEARCH_LOG.md 2026-09-20）。

上界 = 進場價 × (1 + 5% + 來回成本)，也就是賣掉之後真的淨賺 5%。
"""
import sys, time
import numpy as np, pandas as pd
import barrier, validate, study_spec as S

NET = 5.0                                  # 使用者要的淨獲利（%）
UP = (NET + barrier.COST) / 100.0          # 換算成毛報酬的上界
HOLDS = (10, 20, 30)
STOPS = [("2×ATR 停損", "atr"), ("不停損", 0.99)]
IN_END, OOS_END = pd.Timestamp("2019-01-01"), pd.Timestamp("2026-01-01")


COLS = ["date", "code", "label", "net", "mfe", "mae", "day", "regime", "adtv20",
        "bench", "atrp14"]


def _prd(d, min_atrp=0):
    """PRD 四條件的前 20 名（只留個股，標籤只有個股）。

    min_atrp 明確傳入，不跟著網站的預設值走 —— 否則以後改了網站設定，
    RESEARCH_LOG 裡這一輪的數字就再也重現不出來了。
    """
    import shortterm
    f = shortterm.features()
    p = shortterm.params(min_atrp=min_atrp)
    top = shortterm.pick(f, shortterm.score(f, p), p)
    t = f.loc[top.index, ["date", "code"]]
    t["date"] = t["date"].astype("datetime64[ns]")
    return d.merge(t, on=["date", "code"], how="inner")[COLS]


def _top(d, mask, key, asc, k=S.K):
    x = d.loc[mask, COLS].copy()
    x["_k"] = key[mask]
    x = x.dropna(subset=["_k"]).sort_values(["date", "_k"], ascending=[True, asc])
    return x.groupby("date", sort=False).head(k)


def selections(d, u, cs, strat):
    """五種選股。PRD 是網站現在用的四條件。"""
    prd = _prd(d, min_atrp=0)          # 這一輪的基準：當時還沒有波動門檻的版本
    out = {"PRD 無門檻": prd}
    for lim in (0.025, 0.035):
        out["PRD ＋ATR%≥{:.1f}%".format(lim * 100)] = prd[prd["atrp14"] >= lim]
    out["B0r 動能"] = _top(d, *strat[("B0r", "")])
    out["M3 營收事件"] = _top(d, *strat[("M3", "sue≥2.0 公布≤7天")])
    return out


def stats(t, hold):
    if not len(t):
        return {}
    ex = t["net"] - t["bench"]
    daily = t.groupby("date").apply(lambda g: (g["net"] - g["bench"]).mean(), include_groups=False)
    return {"n": len(t),
            "達標%": round(float((t["label"] == 1).mean() * 100), 1),
            "停損%": round(float((t["label"] == -1).mean() * 100), 1),
            "淨每筆": round(float(t["net"].mean()), 2),
            "0050": round(float(t["bench"].mean()), 2),
            "對0050": round(float(ex.mean()), 2),
            "t": round(float(validate.newey_west_t(daily.to_numpy(), lag=hold)[1]), 2)
            if len(daily) > 30 else None}


def main(src):
    d0 = pd.read_pickle(src)
    m = barrier.market()
    rows = []
    for hold in HOLDS:
        for sname, sv in STOPS:
            d = d0.drop(columns=barrier.LABEL_COLS, errors="ignore")
            dn = np.clip(2.0 * d["atrp14"].to_numpy(), 0.02, 0.10) if sv == "atr" else sv
            lab = barrier.labels(d, up=UP, dn=dn, hold=hold)
            d = d.join(lab).assign(bench=barrier.bench_returns(d, lab, m))
            u = S.universe(d)
            cs = S.cand_score(d, u)
            strat = S.strategies(d, u, cs)
            for k, t in selections(d, u, cs, strat).items():
                t = t.dropna(subset=["bench"])
                for period, sel in (("樣本內 08-18", t[t["date"] < IN_END]),
                                    ("樣本外 19-25", t[(t["date"] >= IN_END) & (t["date"] < OOS_END)]),
                                    ("holdout 26", t[t["date"] >= OOS_END])):
                    r = stats(sel, hold)
                    if r:
                        rows.append(dict(持有=hold, 停損=sname, 選股=k, period=period, **r))
    r = pd.DataFrame(rows)
    for period in ("樣本內 08-18", "樣本外 19-25", "holdout 26"):
        x = r[r["period"] == period]
        print("\n===== {} 淨每筆 =====".format(period))
        print(x.pivot_table(index=["持有", "停損"], columns="選股", values="淨每筆", sort=False).round(2).to_string())
        print("----- 達標%（先賺到淨 5%）-----")
        print(x.pivot_table(index=["持有", "停損"], columns="選股", values="達標%", sort=False).round(1).to_string())
        print("----- 對 0050 -----")
        print(x.pivot_table(index=["持有", "停損"], columns="選股", values="對0050", sort=False).round(2).to_string())
    r.to_json("reports/target_study.json", orient="records", force_ascii=False)
    print("\n樣本內最好的 6 組（依淨每筆）：")
    print(r[r.period == "樣本內 08-18"].sort_values("淨每筆", ascending=False).head(6).to_string(index=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    t0 = time.time(); main(sys.argv[1]); print("{:.0f} 秒".format(time.time() - t0))
