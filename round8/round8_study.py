"""第 8 輪（RESEARCH_LOG.md 2026-09-22）：H1–H4 假設。

backtest2 是 shortterm.backtest 的複製版，只加了三個開關：
  pool_mask  濾網加在候選池（取前 N 名之前）          H1、H2
  gap_cap    隔天開盤 > 訊號日收盤 + 1×ATR14 不成交    H4
  staged     碰到目標只賣一半、剩下用移動停利          H3
三個開關全關時，結果必須跟 shortterm.backtest 逐筆完全一致（見 selftest）。
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, ".")
import shortterm as S
import validate

COST, ETF_COST = S.COST, S.ETF_COST


def backtest2(d, p, start=None, end=None, arr=None, bench=None,
              pool_mask=None, gap_cap=False, staged=False, trail_mult=2.0):
    hold = int(p["hold_days"])
    arr = arr or S._forward(d)
    bo, bc = bench if bench is not None else S._bench(d)
    m = pd.Series(True, index=d.index)
    if start is not None:
        m &= d["date"] >= start
    if end is not None:
        m &= d["date"] <= end
    sub = d[m]
    sc = S.score(sub, p)
    if pool_mask is not None:
        sc["liq"] = sc["liq"] & pool_mask.reindex(sub.index).fillna(False).to_numpy()
    top = S.pick(sub, sc, p)
    i0 = top.index.to_numpy()
    n = len(arr["code"])
    code = arr["code"]

    e = i0 + 1
    ok = (e < n)
    ok[ok] = code[e[ok]] == code[i0[ok]]
    ec = np.minimum(e, n - 1)
    sig_close = d.loc[i0, "close"].to_numpy()
    locked = (arr["high"][ec] == arr["low"][ec]) & (arr["open"][ec] > sig_close * 1.09)
    ok &= ~locked
    entry = arr["open"][ec]
    ok &= np.isfinite(entry) & (entry > 0)
    atr_sig = (d.loc[i0, "atr14"]).to_numpy()
    if gap_cap:
        ok &= entry <= sig_close + atr_sig
    stop, target = S.ref_prices(entry, d.loc[i0, "atrp14"], p)

    K = len(i0)
    exit_px = np.full(K, np.nan)      # 全部平倉價（未分批）或後半部價
    exit_i = np.full(K, -1)
    px1 = np.full(K, np.nan)          # 分批：第一批價
    i1 = np.full(K, -1)
    why = np.array([""] * K, dtype=object)
    target_out = target
    target = np.where(np.isfinite(target), target, np.inf)
    hi_seen = np.full(K, -np.inf)
    lo_seen = np.full(K, np.inf)
    hi_raw = np.full(K, -np.inf)      # 移動停利用的原始最高價
    open_ = ok.copy()
    half = np.zeros(K, bool)          # 已賣掉一半
    for j in range(hold):
        idx = np.minimum(e + j, n - 1)
        alive = open_ & (e + j < n)
        alive[alive] &= code[idx[alive]] == code[i0[alive]]
        lost = open_ & ~alive
        open_ &= alive
        o, h, lo, c = (arr[x][idx] for x in ("open", "high", "low", "close"))
        kk = arr["k"][idx] / arr["k"][e]
        hi_seen = np.where(open_, np.maximum(hi_seen, h * kk), hi_seen)
        lo_seen = np.where(open_, np.minimum(lo_seen, lo * kk), lo_seen)

        # 已賣一半的部位：剩下一半的停損 = max(成交價, 最高價 − trail×ATR)，用「前一日為止」的最高價
        if staged:
            st_r = np.maximum(entry, hi_raw - trail_mult * atr_sig)
            stop_eff = np.where(half, st_r, stop)
        else:
            stop_eff = stop
        gap_s = open_ & (o <= stop_eff)
        hit_s = open_ & ~gap_s & (lo <= stop_eff)
        if staged:
            tgt_live = open_ & ~half
            gap_t = tgt_live & ~gap_s & ~hit_s & (o >= target)
            hit_t = tgt_live & ~gap_s & ~hit_s & ~gap_t & (h >= target)
        else:
            gap_t = open_ & ~gap_s & ~hit_s & (o >= target)
            hit_t = open_ & ~gap_s & ~hit_s & ~gap_t & (h >= target)
        if staged:
            # 分批版：最後一天還沒出場的（包含今天才碰到目標、剩下一半）都用收盤價出清
            last = open_ & ~(gap_s | hit_s) & (j == hold - 1)
        else:
            last = open_ & ~(gap_s | hit_s | gap_t | hit_t) & (j == hold - 1)

        if staged:
            # 第一批：目標達成 -> 記錄，部位留一半
            f = gap_t | hit_t
            px = np.where(gap_t, o, target)
            px1[f] = px[f]
            i1[f] = idx[f]
            half |= f
            # 平倉：停損（含移動停利）或時間
            for mask, pxx, label in ((gap_s, o, "停損"), (hit_s, stop_eff, "停損"), (last, c, "時間")):
                mm = mask & open_
                exit_px[mm] = pxx[mm] if np.ndim(pxx) else pxx
                exit_i[mm] = idx[mm]
                # 已賣一半者：標成「達標＋…」
                why[mm] = np.where(half[mm], "分批", label)
                open_ &= ~mm
        else:
            for mask, pxx, label in ((gap_s, o, "停損"), (hit_s, stop, "停損"),
                                     (gap_t, o, "達標"), (hit_t, target, "達標"),
                                     (last, c, "時間")):
                exit_px[mask] = pxx[mask] if np.ndim(pxx) else pxx
                exit_i[mask] = idx[mask]
                why[mask] = label
                open_ &= ~mask
        hi_raw = np.where(open_, np.maximum(hi_raw, h), hi_raw)   # 收盤後才更新：明天的移動停利只看到今天為止的最高價
        exit_px[lost] = np.nan
    done = ok & (exit_i >= 0)

    i0d, ed, x = i0[done], e[done], exit_i[done]
    k_in, k_out = arr["k"][ed], arr["k"][x]
    d_in, d_out = arr["date"][ed], arr["date"][x]
    g2 = exit_px[done] * k_out / (entry[done] * k_in) - 1
    if staged:
        f = i1[done] >= 0
        g1 = np.where(f, px1[done] * arr["k"][np.maximum(i1[done], 0)] / (entry[done] * k_in) - 1, g2)
        gross = np.where(f, 0.5 * g1 + 0.5 * g2, g2)
        x1 = np.where(f, i1[done], x)
        d_out1 = arr["date"][x1]
    else:
        gross = g2
        d_out1 = d_out
    ret = gross * 100 - COST
    mfe = (hi_seen[done] / entry[done] - 1) * 100
    mae = (lo_seen[done] / entry[done] - 1) * 100
    b_in = bo.reindex(pd.DatetimeIndex(d_in)).to_numpy()
    b_out = bc.reindex(pd.DatetimeIndex(d_out)).to_numpy()
    b_out1 = bc.reindex(pd.DatetimeIndex(d_out1)).to_numpy()
    if staged:
        bret = (0.5 * b_out1 + 0.5 * b_out) / b_in - 1
        bret = bret * 100 - ETF_COST
    else:
        bret = (b_out / b_in - 1) * 100 - ETF_COST
    t = pd.DataFrame({
        "signal": arr["date"][i0d], "code": code[i0d],
        "name": d.loc[i0d, "name"].to_numpy(), "kind": d.loc[i0d, "kind"].to_numpy(),
        "score": top.loc[i0d, "score"].to_numpy(), "amt20": top.loc[i0d, "amt20"].to_numpy(),
        "sig_close": sig_close[done], "stop": stop[done], "target": target_out[done],
        "entry_date": d_in, "entry": entry[done], "exit_date": d_out, "exit": exit_px[done],
        "why": why[done], "days": x - ed + 1, "ret": ret, "bench": bret, "excess": ret - bret,
        "mfe": mfe, "mae": mae, "ei": ed, "xi": x})
    return t


def stats(t, hold=20):
    if not len(t):
        return {"n": 0}
    daily = t.groupby("signal")["excess"].mean()
    tt = validate.newey_west_t(daily.to_numpy(), lag=hold)[1] if len(daily) > 30 else np.nan
    return {"n": int(len(t)), "days": int(daily.size),
            "win": round(float((t["ret"] > 0).mean() * 100), 1),
            "avg": round(float(t["ret"].mean()), 2), "bench": round(float(t["bench"].mean()), 2),
            "excess": round(float(t["excess"].mean()), 2),
            "worst": round(float(t["ret"].min()), 1),
            "t": None if not np.isfinite(tt) else round(float(tt), 2),
            "hold": round(float(t["days"].mean()), 1)}


PERIODS = (("樣本內 08-18", "2008-01-01", "2019-01-01"),
           ("樣本外 19-25", "2019-01-01", "2026-01-01"),
           ("2026", "2026-01-01", "2100-01-01"))


def by_period(t, hold=20):
    out = {}
    for name, a, b in PERIODS:
        s = t[(t["signal"] >= a) & (t["signal"] < b)]
        out[name] = stats(s, hold)
    out["全期"] = stats(t, hold)
    return out


def paired(t_arm, t_base, a, b, hold=20):
    """逐訊號日配對差：只取兩邊都有交易的訊號日，差 = 該日（arm 平均超額 − 基準平均超額）。"""
    x = t_arm[(t_arm["signal"] >= a) & (t_arm["signal"] < b)].groupby("signal")["excess"].mean()
    y = t_base[(t_base["signal"] >= a) & (t_base["signal"] < b)].groupby("signal")["excess"].mean()
    j = x.index.intersection(y.index)
    if len(j) < 30:
        return None
    diff = (x.loc[j] - y.loc[j]).to_numpy()
    diff = diff[np.isfinite(diff)]          # 少數日子 0050 沒有對應價格，超額是缺值
    if len(diff) < 30:
        return None
    return {"days": int(len(j)), "diff": round(float(diff.mean()), 3),
            "t": round(float(validate.newey_west_t(diff, lag=hold)[1]), 2)}
