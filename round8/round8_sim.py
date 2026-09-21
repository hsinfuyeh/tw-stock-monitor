"""第 8 輪：有資金限制的組合模擬（RESEARCH_LOG.md 2026-09-22「風險參數」）。

輸入：shortterm.backtest 產生的逐筆交易（含 ei / xi 陣列索引）。
規則：
  * 訊號日隔天開盤進場；同一天最多新增 npd 檔，依網站原本的順序（分數、20 日成交額）
  * 同時最多 N 檔；已持有的代號不重複買
  * 部位金額 = min(前一日收盤淨值 × r ÷ 停損距離, 淨值 × 20%, 可用現金)（不用槓桿）
  * 每日以收盤價盯市；出場當天用實際出場價；來回成本 0.585% 於出場時從部位扣除
"""
import numpy as np
import pandas as pd

COST = 0.585
CAP = 0.20


def prep(trades, arr, dates):
    """每筆交易的每日報酬路徑（日曆索引 + 當日報酬）。"""
    ca = arr["close"] * arr["k"]
    cal = pd.DatetimeIndex(dates)
    T = trades.reset_index(drop=True)
    paths = []
    for r in T.itertuples(index=False):
        e, x = int(r.ei), int(r.xi)
        if x == e:
            rr = np.array([r.exit / r.entry - 1.0])
        else:
            rr = np.empty(x - e + 1)
            rr[0] = arr["close"][e] / r.entry - 1.0
            if x - e > 1:
                rr[1:-1] = ca[e + 1:x] / ca[e:x - 1] - 1.0
            rr[-1] = r.exit * arr["k"][x] / ca[x - 1] - 1.0
        d_idx = cal.searchsorted(pd.DatetimeIndex(arr["date"][e:x + 1]))
        paths.append((d_idx, rr))
    T["dist"] = (T["entry"] - T["stop"]) / T["entry"]
    T["eidx"] = cal.searchsorted(pd.DatetimeIndex(T["entry_date"]))
    return T, paths


def check_paths(T, paths):
    """路徑複利 − 1 必須等於逐筆交易的毛報酬（ret + 成本）。"""
    g = np.array([np.prod(1 + rr) - 1 for _, rr in paths]) * 100
    err = np.abs(g - (T["ret"].to_numpy() + COST))
    return float(np.nanmax(err)), float(np.nanmean(err))


def simulate(T, paths, T_days, N, r, npd, order=None, rng=None):
    """回傳每日淨值（長度 T_days，起始 1.0）與統計。order：候選排序（預設 T 的既有順序）。"""
    by_day = {}
    idxs = np.arange(len(T)) if order is None else order
    for i in idxs:
        by_day.setdefault(int(T["eidx"].iat[i]), []).append(int(i))
    eq = np.ones(T_days)
    cash = 1.0
    open_ = []          # [trade_i, value, notional, pointer]
    held = set()
    taken = 0
    expo = np.zeros(T_days)
    win = 0
    for D in range(T_days):
        eq_prev = eq[D - 1] if D else 1.0
        # 進場
        new = 0
        for i in by_day.get(D, ()):
            if new >= npd or len(open_) >= N:
                break
            code = T["code"].iat[i]
            if code in held:
                continue
            dist = T["dist"].iat[i]
            if not np.isfinite(dist) or dist <= 0:
                continue
            notion = min(eq_prev * r / dist, eq_prev * CAP, cash)
            if notion < 0.002:
                continue
            cash -= notion
            open_.append([i, notion, notion, 0])
            held.add(code)
            new += 1
            taken += 1
        # 盯市
        still = []
        for pos in open_:
            i, val, notion, m = pos
            d_idx, rr = paths[i]
            if m < len(d_idx) and d_idx[m] == D:
                val *= 1 + rr[m]
                m += 1
            if m >= len(d_idx):           # 出場
                fin = val - notion * COST / 100
                cash += fin
                held.discard(T["code"].iat[i])
                win += fin > notion
            else:
                still.append([i, val, notion, m])
        open_ = still
        invested = sum(p[1] for p in open_)
        eq[D] = cash + invested
        expo[D] = invested / eq[D] if eq[D] > 0 else 0
    return eq, expo, taken, win


def metrics(eq, expo, taken, win, start, years):
    ret = np.diff(np.concatenate([[1.0], eq])) / np.concatenate([[1.0], eq[:-1]])
    peak = np.maximum.accumulate(eq)
    mdd = float(((eq / peak) - 1).min() * 100)
    tot = float((eq[-1] - 1) * 100)
    cagr = float((eq[-1] ** (1 / years) - 1) * 100) if eq[-1] > 0 else -100.0
    e10 = eq[10:] / eq[:-10] - 1
    e10 = e10[start:]
    return {"總報酬%": round(tot, 1), "年化%": round(cagr, 1), "最大回落%": round(mdd, 1),
            "10日為正%": round(float((e10 > 0).mean() * 100), 1),
            "10日最差%": round(float(e10.min() * 100), 1),
            "10日第5百分位%": round(float(np.percentile(e10, 5) * 100), 1),
            "平均曝險%": round(float(expo[start:].mean() * 100), 1),
            "成交筆數": int(taken), "勝率%": round(win / taken * 100, 1) if taken else None}


def boot(eq, start, sims=1000, horizon=250, block=20, seed=1):
    """區塊自助法：把日報酬切成 block 天一塊重抽，量 horizon 天的最終報酬與最大回落。"""
    ret = (eq[1:] / eq[:-1] - 1)[start:]
    rng = np.random.default_rng(seed)
    n = len(ret)
    nb = int(np.ceil(horizon / block))
    st = rng.integers(0, n - block, size=(sims, nb))
    idx = (st[:, :, None] + np.arange(block)[None, None, :]).reshape(sims, -1)[:, :horizon]
    path = np.cumprod(1 + ret[idx], axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.ones((sims, 1)), path], axis=1), axis=1)[:, 1:]
    dd = (path / peak - 1).min(axis=1) * 100
    fin = (path[:, -1] - 1) * 100
    return {"250日報酬中位%": round(float(np.median(fin)), 1),
            "250日報酬5百分位%": round(float(np.percentile(fin, 5)), 1),
            "250日虧損機率%": round(float((fin < 0).mean() * 100), 1),
            "250日回落中位%": round(float(np.median(dd)), 1),
            "250日回落95分位%": round(float(np.percentile(dd, 5)), 1)}
