"""排行榜 / 掃描清單。

分成兩類，刻意分開放：

  事實型榜單 —— 不需要任何預測，只是把「已經發生的事」排序。
                今天誰成交最多、誰漲最多、誰爆量、誰創新高、誰快除息。
                這些是可靠的發現工具：用來找出值得進一步研究的標的。

  評分型榜單 —— 需要預測能力才有意義。目前這組因子經 1,869 個交易日驗證
                是反指標（十分位單調性 -0.61、t≈-4.4），所以放在最後，
                而且掛紅色警告。不隱藏它，但也不假裝它有用。
"""
import datetime as dt

import numpy as np
import pandas as pd

import store

# (代號, 標題, 一句話說明, 分類)
# 分頁用短標籤（見 TAB_LABEL），完整標題留在內容區的卡片標題。
# 11 個分頁如果都用完整名稱會塞不下一行，換行後視覺上會散掉。
TAB_LABEL = {
    "amount": "成交額", "gain": "漲幅", "loss": "跌幅", "volume": "爆量",
    "high": "創新高", "low": "創新低", "calm": "低波動", "exdiv": "除權息",
    "inst": "法人買", "instout": "法人賣", "score": "綜合評分",
    "screen": "體質篩選", "pos": "價格位置", "rev": "月營收",
}

SCREENS = [
    ("amount", "成交金額", "今天資金集中在哪裡。量大代表關注度高、也代表你進得去。", "fact"),
    ("gain", "今日漲幅", "今天漲最多的。已經發生的事實，不代表明天會續漲。", "fact"),
    ("loss", "今日跌幅",
     "今天跌最多的。實測取前 40 檔持有 10 日超額 +0.35%（t = 2.07）—— "
     "方向是正的（短期反轉），但小於 0.6% 來回成本，而且下跌的原因往往還沒反映完。", "fact"),
    ("volume", "爆量", "成交量相對前 20 日均量放大最多。有事發生，但不知好壞。", "fact"),
    ("high", "創20日新高", "收盤突破前 20 個交易日最高價。", "fact"),
    ("low", "創20日新低",
     "收盤跌破前 20 個交易日最低價。實測為<b>反指標</b>：取前 40 檔持有 10 日，"
     "超額 -0.39%（t = -2.89），是所有事實型榜單裡唯一顯著落後市場的。", "score"),
    ("calm", "低波動", "波動最小的高流動性股票。適合不想承受大起伏的人。", "fact"),
    ("exdiv", "即將除權息", "未來 60 天內要除權息的。當天價格會調整，那不是下跌。", "fact"),
    ("inst", "法人買超", "三大法人今日買超最多的（已用成交量正規化）。系統唯一通過驗證的真訊號，但效果小於交易成本。", "fact"),
    ("instout", "法人賣超", "三大法人今日賣超最多的。", "fact"),
    ("screen", "體質篩選",
     "套用系統中唯一通過統計校正的兩條排除規則後，剩下的標的。這是排除法不是選股法。", "fact"),
    ("pos", "價格位置高",
     "站在近一年價格區間最高的一群。實測取前 40 檔持有 10 日，超額 +0.78%（t = 3.79），"
     "是所有榜單裡唯一通過多重檢定校正、而且超過交易成本的。", "fact"),
    ("rev", "月營收年增",
     "月營收年增率最高的一群（依法規公布期限對齊，不會偷看未來）。"
     "實測超額 +0.48%（t = 3.28），通過多重檢定校正但仍小於 0.6% 來回成本。"
     "<b>榜首那幾檔的誇張數字要當心</b>：年增率是跟去年同月比，"
     "營建等認列不平均的產業基期可能接近零，會算出四位數的年增率 —— "
     "那是會計時點不是成長。前 40 檔的年增率中位數是 71%，"
     "看中段比看榜首有意義。", "fact"),
    ("score", "綜合評分", "因子合成分數排序。", "score"),
]
SCREEN_MAP = {k: (t, d, c) for k, t, d, c in SCREENS}


def _latest(panel):
    d = panel["date"].max()
    return panel[panel["date"] == d].copy(), d


def _prev_close(panel, day):
    """前一交易日收盤，用來算當日漲跌。"""
    ds = sorted(panel["date"].unique())
    i = ds.index(day)
    if i == 0:
        return {}
    p = panel[panel["date"] == ds[i - 1]]
    return dict(zip(p["code"], p["close"]))


def run_screen(panel, key, limit=40):
    """回傳 (DataFrame, 額外欄位名稱, 額外欄位標題)。"""
    day, dt_ = _latest(panel)
    prev = _prev_close(panel, dt_)
    day["prev"] = day["code"].map(prev)
    day["chg_pct"] = (day["close"] / day["prev"] - 1) * 100

    # 榜單一律套用流動性門檻。沒有這道，榜首永遠是成交幾百股的殭屍股，
    # 那種報酬是買賣價差跳動，而且你也買不到。
    liq = day[day["amt20"] >= 2e7].copy()

    if key == "amount":
        r = day.sort_values("amount", ascending=False).head(limit)
        return r, "amount", "今日成交額"
    # 漲跌幅榜的「今日」欄已經顯示漲跌幅，額外欄位改放量能倍數：
    # 「漲很多但沒量」跟「漲很多且爆量」是完全不同的兩件事。
    if key == "gain":
        r = liq.dropna(subset=["chg_pct"]).sort_values("chg_pct", ascending=False).head(limit)
        return r, "vol_ratio", "量能倍數"
    if key == "loss":
        r = liq.dropna(subset=["chg_pct"]).sort_values("chg_pct").head(limit)
        return r, "vol_ratio", "量能倍數"
    if key == "volume":
        r = liq.dropna(subset=["vol_ratio"]).sort_values("vol_ratio", ascending=False).head(limit)
        return r, "vol_ratio", "量能倍數"
    if key == "high":
        r = liq[liq["close"] > liq["hi20"]].copy()
        r["over"] = (r["close"] / r["hi20"] - 1) * 100
        return r.sort_values("over", ascending=False).head(limit), "over", "超出前高"
    if key == "low":
        r = liq[liq["close"] < liq["lo20"]].copy()
        r["over"] = (r["close"] / r["lo20"] - 1) * 100
        return r.sort_values("over").head(limit), "over", "低於前低"
    if key == "calm":
        r = liq[liq["amt20"] >= 1e8].copy()
        r["annvol"] = r["sd60"] * np.sqrt(252) * 100
        return r.dropna(subset=["annvol"]).sort_values("annvol").head(limit), "annvol", "年化波動"
    if key == "pos":
        # 近一年價格位置。通過 Bonferroni 校正的兩條規則之一，
        # 而且是少數「反過來當選股用也成立」的（見下方 rev 對殖利率的註記）。
        r = liq.dropna(subset=["pos252"]).copy()
        r["posv"] = r["pos252"] * 100
        return r.sort_values("posv", ascending=False).head(limit), "posv", "價格位置"
    if key == "rev":
        # 刻意沒有做「高殖利率榜」。殖利率是全系統 IC 最高的訊號，
        # 但實測取殖利率最高的前 40 檔，超額是 -0.10%（t = -0.60）——
        # 它的資訊全在「排除最低的那一端」，不在「買最高的那一端」。
        # 排除有效不等於選擇有效，這是本系統最重要的一個不對稱。
        r = liq.dropna(subset=["yoy"]).copy()
        return r.sort_values("yoy", ascending=False).head(limit), "yoy", "月營收年增"
    if key == "calm_high":
        r = liq.copy()
        r["annvol"] = r["sd60"] * np.sqrt(252) * 100
        return (r.dropna(subset=["annvol"]).sort_values("annvol", ascending=False)
                .head(limit), "annvol", "年化波動")
    return day.head(0), None, None


def upcoming_exdiv(panel, days=60, limit=60):
    """未來 N 天內除權息的標的。這是行事曆，不是預測。"""
    today = pd.Timestamp(dt.date.today())
    try:
        ex = store.q("SELECT date, code, value, prev_close, ref_price, kind FROM exrights")
    except Exception:
        return pd.DataFrame()
    ex["date"] = pd.to_datetime(ex["date"])
    fut = ex[(ex["date"] >= today) & (ex["date"] <= today + pd.Timedelta(days=days))].copy()
    if not len(fut):
        return fut
    day, _ = _latest(panel)
    info = day.set_index("code")[["name", "close", "amt20"]]
    fut = fut.join(info, on="code", how="inner")
    fut["yield_pct"] = (fut["prev_close"] - fut["ref_price"]) / fut["prev_close"] * 100
    fut["days_left"] = (fut["date"] - today).dt.days
    return fut.sort_values("date").head(limit)
