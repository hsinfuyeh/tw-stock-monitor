"""Arm 8：用模型選股（RESEARCH_LOG 第 9 輪，事前登記的定義，不要改）。

做什麼：邏輯迴歸估「隔天開盤買、持有 20 個交易日後，贏過同期 0050 超過交易成本」的機率。
網站的四條件打分是人工定的權重；這裡改讓資料決定每個特徵佔多少。

最重要的是**不偷看未來**：
  - 每年 1 月 1 日重訓一次，只用「標籤已經確定」的資料 —— 標籤要 20 個交易日後才知道，
    所以只取結束日早於那天 30 個日曆日以前的列。
  - 某一年的預測只用那年以前訓練的模型。2008–2010 沒有模型，不選股。
  - 今天（2026）的預測用的是 2026-01-01 訓練的模型，跟回測裡 2026 年用的是同一個。

用 numpy 自己寫牛頓法，不多裝套件：13 個特徵、幾十萬筆，一次訓練一兩秒。
"""
import numpy as np
import pandas as pd

HORIZON = 20            # 標籤的持有天數
COST = 0.585            # 贏 0050 要超過這個才算（%）
FIRST_YEAR = 2011
MAX_ROWS = 400_000
L2 = 1e-3
SEED = 0

FEATS = ["f_ma20", "f_ma5_20", "f_hi20", "f_vol", "f_sf", "f_st", "f_yoy", "f_yoy_na",
         "f_hi12", "f_atrp", "f_adtv", "f_z5", "f_ret20", "f_down"]


def design(d):
    """特徵矩陣（每列一個股票日）。全部只用當天收盤就知道的東西。"""
    x = pd.DataFrame(index=d.index)
    x["f_ma20"] = d["close"] / d["ma20"] - 1
    x["f_ma5_20"] = d["ma5"] / d["ma20"] - 1
    x["f_hi20"] = d["close"] / d["hi20_prev"] - 1
    x["f_vol"] = np.log((d["volume"] + 1) / (d["vol5_prev"] + 1))
    x["f_sf"] = d["streak_f"].clip(upper=20)
    x["f_st"] = d["streak_t"].clip(upper=20)
    x["f_yoy"] = d["yoy"].clip(-100, 300).fillna(0)
    x["f_yoy_na"] = d["yoy"].isna().astype(float)
    x["f_hi12"] = (d["rev_high12"] == 1).astype(float)
    x["f_atrp"] = d["atrp14"]
    x["f_adtv"] = np.log(d["adtv20"].clip(lower=1))
    x["f_z5"] = d["ret5_z"]
    x["f_ret20"] = d["adj"] / d.groupby("code", sort=False)["adj"].shift(20) - 1
    x["f_down"] = (d["regime"] == "down").astype(float)
    return x[FEATS].astype(float)


def labels(d):
    """y：隔天開盤買、第 HORIZON 個交易日收盤賣，減掉同期 0050 > COST。還有標籤的結束日。"""
    g = d.groupby("code", sort=False)
    buy = g["open"].shift(-1) * g["k"].shift(-1)
    sell = g["close"].shift(-HORIZON) * g["k"].shift(-HORIZON)
    end = g["date"].shift(-HORIZON)
    ret = (sell / buy - 1) * 100
    b = d[d["code"] == "0050"].set_index("date")
    bo = (b["open"] * b["k"]).shift(-1)
    bc = (b["close"] * b["k"]).shift(-HORIZON)
    bret = (bc / bo - 1) * 100
    ex = ret - d["date"].map(bret).to_numpy()
    y = (ex > COST).astype(float).where(ex.notna())
    return y, end


def fit(X, y):
    """L2 邏輯迴歸，牛頓法。X 已標準化。"""
    X1 = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(X1.shape[1])
    reg = np.full(X1.shape[1], L2 * len(X))
    reg[0] = 0
    for _ in range(25):
        p = 1 / (1 + np.exp(-np.clip(X1 @ w, -30, 30)))
        grad = X1.T @ (p - y) + reg * w
        H = (X1 * (p * (1 - p))[:, None]).T @ X1 + np.diag(reg)
        step = np.linalg.solve(H, grad)
        w -= step
        if np.abs(step).max() < 1e-7:
            break
    return w


def predict(w, X):
    X1 = np.column_stack([np.ones(len(X)), X])
    return 1 / (1 + np.exp(-np.clip(X1 @ w, -30, 30)))


def walk_forward(d):
    """整個面板的 mprob（樣本外機率）。範圍外、或 2011 年以前的列是 NaN。

    回傳 (mprob Series, 每年模型的係數表)。係數表存起來，網頁可以顯示「模型在看什麼」。
    """
    uni = (d["kind"] == "個股") & (d["vol20_lots"] >= 1000)
    X = design(d)
    ok = uni & X.notna().all(axis=1)
    y, end = labels(d)
    out = pd.Series(np.nan, index=d.index)
    coefs = {}
    rng = np.random.default_rng(SEED)
    years = sorted(set(d.loc[ok, "date"].dt.year))
    for yr in [y_ for y_ in years if y_ >= FIRST_YEAR]:
        cut = pd.Timestamp(yr, 1, 1)
        tr = ok & y.notna() & (end < cut - pd.Timedelta(days=30))
        idx = np.flatnonzero(tr.to_numpy())
        if len(idx) < 5000:
            continue
        if len(idx) > MAX_ROWS:
            idx = np.sort(rng.choice(idx, MAX_ROWS, replace=False))
        Xt = X.to_numpy()[idx]
        mu, sd = Xt.mean(0), Xt.std(0)
        sd[sd == 0] = 1
        Z = np.clip((Xt - mu) / sd, -5, 5)
        w = fit(Z, y.to_numpy()[idx])
        pr = ok & (d["date"].dt.year == yr)
        pi = np.flatnonzero(pr.to_numpy())
        Zp = np.clip((X.to_numpy()[pi] - mu) / sd, -5, 5)
        out.iloc[pi] = predict(w, Zp)
        coefs[yr] = dict(zip(["const"] + FEATS, np.round(w, 4).tolist()),
                         n=int(len(idx)), base=round(float(y.to_numpy()[idx].mean()), 4))
    return out, coefs
