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
    "amount": "成交金額", "gain": "漲最多", "loss": "跌最多", "volume": "量暴增",
    "high": "創 20 日新高", "low": "創 20 日新低", "calm": "波動小", "exdiv": "除權息",
    "inst": "法人買", "instout": "法人賣", "score": "綜合評分",
    "screen": "體質篩選", "pos": "接近一年高點", "rev": "營收成長",
    "sue": "營收優於預期",
}

# 用詞原則：一般投資人看得懂。不出現 t 值、標準差、因子、宇宙這類詞；
# 統計數字改用「過去 N 年、平均比大盤多/少幾 %」講，嚴謹的細節放在「為什麼？」展開裡。
SCREENS = [
    ("amount", "成交金額最大",
     "今天資金集中在哪裡。成交金額大代表很多人在關注，也比較好買賣。", "fact"),
    ("gain", "今天漲最多",
     "今天漲幅最大的。已經漲完是事實，不代表明天還會漲。", "fact"),
    ("loss", "今天跌最多",
     "今天跌幅最大的。過去統計這些股票之後 10 天平均小幅反彈（比大盤多 0.35%），"
     "但不夠付手續費和稅，而且下跌的原因可能還沒反映完。", "fact"),
    ("volume", "成交量暴增",
     "今天成交量比平常（前 20 天平均）放大最多的。代表有事發生，但不知道是好事還是壞事。", "fact"),
    ("high", "創 20 日新高", "收盤價高過前 20 個交易日的最高價。", "fact"),
    ("low", "創 20 日新低",
     "收盤價跌破前 20 個交易日的最低價。過去統計這些股票之後 10 天平均"
     "<b>比大盤差 0.39%</b>，而且不像是巧合 —— 是少數明確「之後偏弱」的名單。", "score"),
    ("calm", "股價波動最小",
     "股價起伏最小、又有一定成交量的股票。適合不想承受大起伏的人。", "fact"),
    ("exdiv", "即將除權息",
     "未來 60 天內要除權息的。除權息當天股價會往下調整，"
     "那是發給股東的部分，不是下跌。", "fact"),
    ("inst", "法人買最多",
     "外資、投信、自營商今天買最多的（換算成平常成交量的比例，大小股票才能比）。"
     "過去統計方向是對的，但幅度小於手續費和稅。", "fact"),
    ("instout", "法人賣最多", "外資、投信、自營商今天賣最多的。", "fact"),
    ("screen", "體質篩選",
     "用兩條有回測支持的排除規則刷掉一部分股票後，剩下的名單。這是排除，不是推薦。", "fact"),
    ("pos", "股價接近一年高點",
     "股價站在近一年高低區間最上面的一群（不是「漲最多」的名單）。"
     "過去 7 年統計，這些股票之後 10 天平均比大盤多 0.78%，"
     "是這裡少數扣掉手續費和稅還有剩的名單。", "fact"),
    ("rev", "月營收成長最多",
     "月營收跟去年同月比成長最多的（只用當時已經公布的營收，不會偷看未來）。"
     "過去統計之後 10 天平均比大盤多 0.48%，方向可信，但還不夠付手續費和稅。"
     "<b>排最前面的幾檔要小心</b>：去年同月營收接近零的公司（例如建設公司）"
     "會算出好幾千 % 的成長，那是入帳時間造成的，不是真的成長。看中段比看榜首有意義。", "fact"),
    ("sue", "月營收優於預期",
     "這個月營收的成長，比公司<b>自己平常</b>的成長好最多的一群 —— "
     "不是「成長最快」，是「比自己平常好最多」。這裡的「預期」是公司自己過去 12 個月的"
     "成長率，不是分析師預估。過去統計（2008 年起）這些股票之後 40 個交易日平均比大盤多 1.6%、"
     "60 天多 2.2%，<b>是這裡唯一明顯超過手續費和稅的名單</b>。", "fact"),
    ("score", "綜合評分",
     "把多個指標加總成一個分數排序。過去統計<b>分數越高、之後表現越差</b>，不要照著買。",
     "score"),
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
        return r, "vol_ratio", "成交量倍數"
    if key == "loss":
        r = liq.dropna(subset=["chg_pct"]).sort_values("chg_pct").head(limit)
        return r, "vol_ratio", "成交量倍數"
    if key == "volume":
        r = liq.dropna(subset=["vol_ratio"]).sort_values("vol_ratio", ascending=False).head(limit)
        return r, "vol_ratio", "成交量倍數"
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
        return r.dropna(subset=["annvol"]).sort_values("annvol").head(limit), "annvol", "一年波動幅度"
    if key in ("inst", "instout"):
        # 這兩個原本只存在於 server.py 的路由裡，run_screen 會落到最後一行
        # 回傳空表 —— 靜態站因此產出了兩份 0 筆的法人榜，而且沒有錯誤訊息。
        # 跟 score 是同一類問題：路由裡的特例，就是靜態站的靜默缺口。
        r = liq.dropna(subset=["法人買超"])
        return (r.sort_values("法人買超", ascending=(key == "instout")).head(limit),
                "法人買超", "法人買超")
    if key == "sue":
        # 事件驅動：漂移會隨時間衰減，所以只留最近公布的。
        # 45 天約等於一個公布週期 —— 超過就代表下一期數字快出來了。
        r = liq.dropna(subset=["sue"]).copy()
        if "days_since" in r.columns:
            r = r[r["days_since"] <= 45]
        return r.sort_values("sue", ascending=False).head(limit), "sue", "超出平常幅度"
    if key == "pos":
        # 近一年價格位置。通過 Bonferroni 校正的兩條規則之一，
        # 而且是少數「反過來當選股用也成立」的（見下方 rev 對殖利率的註記）。
        r = liq.dropna(subset=["pos252"]).copy()
        r["posv"] = r["pos252"] * 100
        return r.sort_values("posv", ascending=False).head(limit), "posv", "一年區間位置"
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
                .head(limit), "annvol", "一年波動幅度")
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


# 各榜單的警語。結構化放在這裡，是為了讓本機版（webui.statusbar）與
# 靜態站（publish -> meta.json -> lists.html）用<b>同一份文字</b>。
# 之前這段是寫死在 server.py 的路由裡，靜態站就只能各抄一份 ——
# 抄了之後兩邊會各自漂移，而漂移的那一邊沒有人會發現。
NOTES = {}

for _k in ['score']:
    NOTES[_k] = (
        "bad", "<b>這份名單過去的表現是反的，不要照著買。</b>",
        "用 2019–2026 年約 1,870 個交易日的資料回頭測試：<b>分數越高的股票，"
        "之後表現越差</b>，而且差距大到不像巧合。這不是「效果不明顯」，是方向相反。<br><br>"
        "留在這裡是提醒：<b>一份看起來很有道理的「選股分數」，實際上可能完全是反的</b>。"
        "沒有回頭測試過的分數，都不該直接相信。")

for _k in ['low']:
    NOTES[_k] = (
        "bad", "<b>這份名單之後的表現明顯比大盤差。</b>",
        "過去 7 年、148 個互不重疊的日期回頭測試：取這份名單前 40 檔持有 10 天，"
        "<b>平均比大盤差 0.39%</b>，是所有「今日行情」名單裡唯一明顯落後的。<br><br>"
        "它的用處是<b>提醒你哪些先不要碰</b>。想反過來靠它賺錢要放空，"
        "但台股放空限制多（平盤以下不能放空、要付借券費用、可能被迫回補），實際上很難做。")

for _k in ['sue']:
    NOTES[_k] = (
        "ok", "這是這裡<b>唯一明顯超過手續費和稅</b>的名單。",
        "<b>「預期」指的是什麼。</b>一般講「優於預期」通常是跟分析師預估比，"
        "但這裡沒有分析師資料，改成跟<b>公司自己</b>比：拿這個月的營收年增率，"
        "減掉它過去 12 個月的平均年增率，再除以它平常的起伏幅度。"
        "所以一家營收一向很穩的公司突然跳一截，會比一家營收本來就忽高忽低的公司"
        "跳同樣幅度更顯眼。<br><br>"
        "<b>跟「營收成長」名單不一樣。</b>營收成長問的是「長得快不快」，"
        "這份問的是「比自己平常好多少」。一家連續三年年增 60% 的公司，"
        "這個月公布 40%，在營收成長名單上仍然名列前茅 —— 但對它自己來說是變差了，"
        "過去統計這種股票之後會偏弱。<br><br>"
        "<b>過去表現（2008 年起 222 個營收月份，名單前 20%）：</b><br>"
        "&nbsp;&nbsp;持有 10 天　比大盤多 0.29%<br>"
        "&nbsp;&nbsp;持有 20 天　比大盤多 0.83%<br>"
        "&nbsp;&nbsp;持有 40 天　比大盤多 1.58%<br>"
        "&nbsp;&nbsp;持有 60 天　比大盤多 2.18%<br><br>"
        "好處隨持有時間<b>慢慢累積</b>，持有 40、60 天時 19 年<b>每一年都是正的</b>。"
        "而且 2008–2018 這 11 年是這個訊號被發現之後才補進來的資料，"
        "完全沒參與過設計，結果跟 2019 年後一致。"
        "學術上這叫 PEAD（公布好消息之後，股價會持續往同方向走一段時間），"
        "是全世界研究最多、最穩定的現象之一。<br><br>"
        "<b>怎麼用。</b>效果會隨時間變淡，所以名單只留公布 45 天內的。"
        "適合抱 <b>40–60 個交易日</b>，不是短進短出 —— 抱 10 天的 0.29% 還不夠付 0.6% 的手續費和稅。"
        "<br><br><b>仍要注意</b>：「比大盤」是跟同一天所有股票的平均比。改跟 0050 比、"
        "扣完手續費和稅，持有 60 天平均多 1.3%，但差距還不夠確定（t 約 2）—— "
        "因為一般個股本來就常輸 0050。過去有效不代表以後一定有效。")

for _k in ['pos', 'rev']:
    NOTES[_k] = (
        "ok", "這兩份名單的過去表現<b>不像是巧合</b>。",
        "把這一頁每一份名單都用同樣方法回頭測試（過去 7 年、148 個互不重疊的日期，"
        "取前 40 檔持有 10 天，跟當天全市場平均比）：<br><br>"
        "<b>股價接近一年高點　平均比大盤多 0.78%</b> —— 扣掉手續費和稅還有剩。<br>"
        "<b>月營收成長最多　　平均比大盤多 0.48%</b> —— 方向可信，但不夠付手續費和稅。<br><br>"
        "其他名單（成交金額、法人買、跌最多…）看起來也有一點效果，"
        "但一次測 15 份名單，本來就會有幾份碰巧看起來有效；"
        "把這個運氣成分扣掉之後，只剩這兩份站得住。<br><br>"
        "<b>「接近一年高點」不等於「漲最多」。</b>過去一年漲最多的股票（常聽到的「動能」）"
        "在台股測不出效果；看的是現在離一年高點多近。<br><br>"
        "<b>為什麼沒有「高殖利率」名單</b>：殖利率最高的前 40 檔，之後平均反而比大盤差 0.10%。"
        "殖利率有用的是另一端 —— <b>排除殖利率最低的</b>比較有效，挑最高的沒有用。")

for _k in ['screen']:
    NOTES[_k] = (
        "ok", "這兩條排除規則有回測支持。",
        "<b>規則一：刷掉殖利率最低的 40%</b> —— 剩下的股票，一般情況下（中位數）"
        "每年多 5.5%。<br>"
        "<b>規則二：刷掉股價在一年區間最低 30% 的</b> —— 剩下的股票平均每年多 2.9%。<br><br>"
        "<b>為什麼「排除」比「挑選」容易有效</b>：挑選要先贏過 0.6% 的手續費和稅，"
        "排除不用付任何錢。<br><br><b>但這不是買進清單</b>，只代表「這些沒被刷掉」。")

for _k in ['inst', 'instout']:
    NOTES[_k] = (
        "warn", "法人買賣方向有參考價值，但<b>效果小於手續費和稅</b>。",
        "過去統計，法人買越多的股票，之後表現確實越好，越往上越明顯。"
        "但差距很小：5 天大約 0.23%，而 5 天換一次股票的手續費和稅加起來約 0.97%。<br><br>"
        "所以它適合<b>當作參考</b>（「今天法人在買這檔」是有意義的事實），"
        "不適合單獨拿來決定買賣。")
del _k


def score_rows(panel, limit=25):
    """綜合評分榜：分數最高與最低各 limit 檔。

    這個榜單的資料來源本來只存在於 server.py 的路由裡，所以 run_screen
    對 'score' 會落到最後一行回傳空表 —— 靜態站因此產出了一份 0 筆的
    綜合評分頁，而且沒有任何錯誤。抽到這裡讓兩邊共用同一份。
    """
    import rank as rankmod
    from run import CORE
    use = [f for f in CORE if f in panel.columns]
    score, _ = rankmod.composite(panel, use)
    day = panel.assign(_s=score)
    day = day[day["date"] == day["date"].max()].dropna(subset=["_s"])
    prev = _prev_close(panel, day["date"].iloc[0])
    day["prev"] = day["code"].map(prev)
    day["chg_pct"] = (day["close"] / day["prev"] - 1) * 100
    return (day.sort_values("_s", ascending=False).head(limit),
            day.sort_values("_s").head(limit))
