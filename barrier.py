"""10 日 / +5% 先觸及標籤（triple-barrier）與規格書的特徵字典。

依《短線選股工具_研究驗證與實作規格_2026-09-18》：

  標籤    訊號日收盤算完 -> 隔天開盤進場 -> 10 個交易日內
          先碰 +5% 記 +1、先碰停損記 -1、都沒碰到記 0（timeout，保留第 10 天的報酬）
  跳空    開盤就穿過停損／目標，以開盤價成交，不假設剛好成交在界線上
  同日    同一根日 K 同時碰到兩邊，無法得知先後 -> 保守算停損先到
  鎖死    隔天開盤一字漲停買不到 -> 不成交；持有中遇到整天鎖跌停 -> 當天賣不掉，順延
  除權息  界線與損益都用總報酬係數 k 還原，除息日的價格跳空不會被當成碰到停損

特徵只用「當天收盤時已經知道的資料」，滾動視窗凡是當作比較基準的都不含當日。
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

import factors

ROOT = Path(__file__).parent
COST = 0.585                      # 來回：手續費 0.1425% × 2 + 證交稅 0.3%

# TWSE 產業別代碼（ref/industry.json 的來源 t187ap03_L 用的是代碼）
IND_NAME = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維", "05": "電機機械",
    "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙", "10": "鋼鐵", "11": "橡膠",
    "12": "汽車", "14": "建材營造", "15": "航運", "16": "觀光餐旅", "17": "金融保險",
    "18": "貿易百貨", "20": "其他", "21": "化學", "22": "生技醫療", "23": "油電燃氣",
    "24": "半導體", "25": "電腦及週邊", "26": "光電", "27": "通信網路", "28": "電子零組件",
    "29": "電子通路", "30": "資訊服務", "31": "其他電子", "35": "綠能環保", "36": "數位雲端",
    "37": "運動休閒", "38": "居家生活", "91": "存託憑證",
}


def industry_map():
    """代號 -> 產業別代碼。

    限制：這是目前的分類（不是歷史當時的），已下市的公司沒有分類。
    公司改產業別的情況很少，對 20 日產業強弱的影響有限，但要知道這不是 point-in-time。"""
    j = json.loads((ROOT / "ref" / "industry.json").read_text(encoding="utf-8"))
    return j["industry"]


# --------------------------------------------------------------------- 市場
def market():
    """大盤狀態（用 0050 總報酬指數當大盤）。

      regime  up    收盤 > 60 日均 且 20 日均 > 60 日均
              down  收盤 < 60 日均 且 20 日均 < 60 日均
              chop  其他
      highvol 20 日報酬波動在過去 500 個交易日的前 20%
    """
    e = factors.load_panel("etf", ("domestic",))
    m = e[e["code"] == "0050"].sort_values("date").set_index("date")
    px = m["adj"]
    out = pd.DataFrame(index=m.index)
    out["m_px"] = px
    out["m_ret5"] = px / px.shift(5) - 1
    out["m_ret20"] = px / px.shift(20) - 1
    out["m_ret60"] = px / px.shift(60) - 1
    ma20, ma60 = px.rolling(20).mean(), px.rolling(60).mean()
    up = (px > ma60) & (ma20 > ma60)
    dn = (px < ma60) & (ma20 < ma60)
    out["regime"] = np.where(up, "up", np.where(dn, "down", "chop"))
    out.loc[ma60.isna(), "regime"] = None
    rv = np.log(px).diff().rolling(20).std()
    out["m_rv20"] = rv
    out["highvol"] = rv > rv.rolling(500, min_periods=250).quantile(0.8)
    out["m_open"], out["m_close"], out["m_k"] = m["open"], m["close"], m["adj"] / m["close"]
    return out


# --------------------------------------------------------------------- 特徵
def features(with_revenue=True):
    """個股（上市普通股）的特徵面板。"""
    d = factors.load_panel("common").sort_values(["code", "date"]).reset_index(drop=True)
    d["date"] = d["date"].astype("datetime64[ns]")
    g = d.groupby("code", sort=False)
    d["k"] = d["adj"] / d["close"]
    prev = g["close"].shift(1)
    d["prev_close"] = d["ref_price"].where(d["ref_price"].notna(), prev)

    roll = lambda col, n, fn, shift=0: g[col].transform(
        lambda s: getattr((s.shift(shift) if shift else s).rolling(n, min_periods=n), fn)())

    # 流動性：成交額比股數可比；中位數比平均穩（不被單日爆量拉高）
    d["adtv20"] = roll("amount", 20, "median")
    d["adtv5"] = roll("amount", 5, "median")
    d["dollar_vol_accel"] = d["adtv5"] / d["adtv20"]

    # 趨勢與相對強度（總報酬）
    for n in (5, 20, 60):
        d["ret{}".format(n)] = d["adj"] / g["adj"].shift(n) - 1
    d["high52_ratio"] = d["adj"] / g["adj"].transform(
        lambda s: s.rolling(252, min_periods=200).max())

    # 量能：今天的量 / 前 20 日量的中位數（不含今天）
    d["rvol20"] = d["volume"] / roll("volume", 20, "median", shift=1)

    # 波動：報酬標準差與 ATR 分開（規格書 5.2：ATR 不是報酬標準差）
    d["logr"] = np.log(d["adj"]).groupby(d["code"]).diff()
    d["rv20"] = g["logr"].transform(lambda s: s.rolling(20, min_periods=20).std())
    tr = pd.concat([d["high"] - d["low"], (d["high"] - d["prev_close"]).abs(),
                    (d["low"] - d["prev_close"]).abs()], axis=1).max(axis=1)
    d["tr"] = tr
    d["atr14"] = d.groupby("code", sort=False)["tr"].transform(
        lambda s: s.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())
    d["atrp14"] = d["atr14"] / d["close"]

    # 均線與延伸
    for n in (20, 60):
        d["ma{}".format(n)] = roll("close", n, "mean")
    d["ext_ma20_atr"] = (d["close"] - d["ma20"]) / d["atr14"]

    # 收斂：10 日高低區間 / ATR，跟自己過去一年比（前 30% 算收斂）
    d["range10_atr"] = (roll("high", 10, "max") - roll("low", 10, "min")) / d["atr14"]
    q30 = d.groupby("code", sort=False)["range10_atr"].transform(
        lambda s: s.shift(1).rolling(250, min_periods=120).quantile(0.3))
    d["compressed"] = d["range10_atr"] <= q30

    # 突破與回檔
    d["hi20_prev"] = roll("high", 20, "max", shift=1)
    d["breakout_dist"] = d["close"] / d["hi20_prev"] - 1
    d["pullback_dd"] = d["close"] / roll("close", 20, "max") - 1
    d["prev_high"] = g["high"].shift(1)

    # 短期反轉：5 日報酬相對自己過去一年的 z 值（不含今天）
    mu = d.groupby("code", sort=False)["ret5"].transform(
        lambda s: s.shift(1).rolling(250, min_periods=120).mean())
    sd = d.groupby("code", sort=False)["ret5"].transform(
        lambda s: s.shift(1).rolling(250, min_periods=120).std())
    d["ret5_z"] = (d["ret5"] - mu) / sd
    d["max_daily_ret20"] = g["ret"].transform(lambda s: s.rolling(20, min_periods=20).max())
    d["gap_pct"] = d["open"] / d["prev_close"] - 1

    # 大盤與產業
    m = market()
    d = d.merge(m[["m_ret20", "m_ret60", "regime", "highvol"]], left_on="date",
                right_index=True, how="left")
    d["rs_mkt20"] = d["ret20"] - d["m_ret20"]
    d["rs_mkt60"] = d["ret60"] - d["m_ret60"]
    ind = industry_map()
    d["ind"] = d["code"].map(ind)
    gi = d.groupby(["date", "ind"])["ret20"]
    n_ind = gi.transform("count")
    med = gi.transform("median").where(n_ind >= 5)
    d["industry_ret20"] = med - d["m_ret20"]
    d["rs_ind20"] = d["ret20"] - med

    # 月營收事件（M3）：優於預期程度與公布後天數
    if with_revenue:
        import revenue
        rv = revenue.load()
        if len(rv):
            pit = revenue.as_of_panel(rv, d["date"].unique())
            pit["date"] = pit["date"].astype("datetime64[ns]")
            d = d.merge(pit[["date", "code", "yoy", "sue", "days_since"]],
                        on=["date", "code"], how="left")
    d = d.sort_values(["code", "date"]).reset_index(drop=True)
    return d


# --------------------------------------------------------------------- 標籤
def labels(d, up=0.05, dn=0.03, hold=10, cost=COST):
    """每一列（訊號日）的 10 日先觸及標籤。d 必須依 code, date 排序且索引連續。

    dn 可以是常數或與 d 等長的陣列（例如 ATR 型停損）。
    回傳 DataFrame（與 d 同索引）：
      label   +1 先碰目標、-1 先碰停損、0 到期；進不了場或資料不足為 NaN
      day     出場是持有的第幾天（1 起算）
      net     扣成本後報酬（%）
      mfe/mae 持有期間最大有利／不利幅度（%，相對進場價）
    """
    n = len(d)
    code = d["code"].to_numpy()
    o, h, lo, c, k = (d[x].to_numpy(dtype=float) for x in ("open", "high", "low", "close", "k"))
    pc = d["prev_close"].to_numpy(dtype=float)
    i = np.arange(n)
    e = i + 1
    ok = e < n
    ec = np.minimum(e, n - 1)
    ok &= code[ec] == code
    # 隔天開盤一字漲停：買不到
    locked_up = (h[ec] == lo[ec]) & (o[ec] >= pc[ec] * 1.095)
    ok &= ~locked_up & np.isfinite(o[ec]) & (o[ec] > 0)
    E = o[ec] * k[ec]
    dn = np.broadcast_to(np.asarray(dn, dtype=float), (n,))
    U, L = E * (1 + up), E * (1 - dn)

    label = np.full(n, np.nan)
    day = np.full(n, np.nan)
    px = np.full(n, np.nan)
    mfe = np.full(n, -np.inf)
    mae = np.full(n, np.inf)
    live = ok.copy()
    for j in range(hold):
        idx = np.minimum(e + j, n - 1)
        valid = live & (e + j < n)
        valid[valid] &= code[idx[valid]] == code[valid]
        # 持有期還沒走完（資料到底了）-> 不給標籤
        live &= valid
        O, H, Lw, C = o[idx] * k[idx], h[idx] * k[idx], lo[idx] * k[idx], c[idx] * k[idx]
        mfe = np.where(live, np.maximum(mfe, H), mfe)
        mae = np.where(live, np.minimum(mae, Lw), mae)
        # 整天鎖跌停：賣不掉，今天不出場
        stuck = (h[idx] == lo[idx]) & (c[idx] <= pc[idx] * 0.905)
        later = j > 0          # 第一天的開盤就是進場價，沒有跳空的問題
        gs = live & ~stuck & later & (O <= L)
        hs = live & ~stuck & ~gs & (Lw <= L)
        gt = live & ~gs & ~hs & later & (O >= U)
        ht = live & ~gs & ~hs & ~gt & (H >= U)
        to = live & ~gs & ~hs & ~gt & ~ht & (j == hold - 1)
        for mask, p, lab in ((gs, O, -1), (hs, L, -1), (gt, O, 1), (ht, U, 1), (to, C, 0)):
            px[mask] = p[mask]
            label[mask] = lab
            day[mask] = j + 1
            live &= ~mask
    done = np.isfinite(label)
    net = np.where(done, (px / E - 1) * 100 - cost, np.nan)
    # 進場／出場的位置索引，讓呼叫端可以對齊同期間的 0050（或其他基準）
    ei = np.where(done, e, -1)
    xi = np.where(done, e + np.nan_to_num(day - 1, nan=0).astype(int), -1)
    return pd.DataFrame({
        "label": label, "day": day, "net": net,
        "mfe": np.where(done, (mfe / E - 1) * 100, np.nan),
        "mae": np.where(done, (mae / E - 1) * 100, np.nan),
        "ei": ei, "xi": xi,
    }, index=d.index)


ETF_COST = 0.1425 * 2 + 0.1        # 0050 來回（證交稅 0.1%）


def bench_returns(d, lab, m=None):
    """每筆交易「同一段時間改買 0050」的報酬（%，已扣 0050 的成本）。"""
    m = market() if m is None else m
    dates = d["date"].to_numpy()
    ei, xi = lab["ei"].to_numpy(), lab["xi"].to_numpy()
    ok = ei >= 0
    d_in = pd.DatetimeIndex(np.where(ok, dates[np.clip(ei, 0, len(d) - 1)], dates[0]))
    d_out = pd.DatetimeIndex(np.where(ok, dates[np.clip(xi, 0, len(d) - 1)], dates[0]))
    b_in = (m["m_open"] * m["m_k"]).reindex(d_in).to_numpy()
    b_out = (m["m_close"] * m["m_k"]).reindex(d_out).to_numpy()
    return pd.Series(np.where(ok, (b_out / b_in - 1) * 100 - ETF_COST, np.nan), index=d.index)
