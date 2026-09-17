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
    "screen": "體質篩選", "pos": "動能", "rev": "月營收", "sue": "優於預期",
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
    ("pos", "動能：52 週高點接近度",
     "站在近一年價格區間最高的一群，也就是金融學說的「52 週高點動能」"
     "（George &amp; Hwang, 2004）。實測取前 40 檔持有 10 日，超額 +0.78%"
     "（t = 3.79），是所有榜單裡唯一通過多重檢定校正、而且超過交易成本的。", "fact"),
    ("rev", "月營收年增",
     "月營收年增率最高的一群（依法規公布期限對齊，不會偷看未來）。"
     "實測超額 +0.48%（t = 3.28），通過多重檢定校正但仍小於 0.6% 來回成本。"
     "<b>榜首那幾檔的誇張數字要當心</b>：年增率是跟去年同月比，"
     "營建等認列不平均的產業基期可能接近零，會算出四位數的年增率 —— "
     "那是會計時點不是成長。前 40 檔的年增率中位數是 71%，"
     "看中段比看榜首有意義。", "fact"),
    ("sue", "月營收優於預期（公布後漂移）",
     "月營收<b>超出公司自己常態</b>最多的一群 —— 不是「成長最快」，是「比自己過去好最多」。"
     "這裡的「預期」指的是<b>公司自己過去 12 個月的常態</b>，不是分析師預估"
     "（本系統沒有分析師資料）。"
     "這是 PEAD（盈餘公布後漂移）的月頻版本，全球記錄最完整的異常之一。"
     "實測做多最高 20% 這一組：持有 40 日超額 +2.30%（t = 5.67）、"
     "60 日 +3.66%（t = 4.91），<b>是本系統第一個真正跨過 0.6% 交易成本的選股訊號</b>。", "fact"),
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
        return r.sort_values("sue", ascending=False).head(limit), "sue", "超出預期"
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


# 各榜單的警語。結構化放在這裡，是為了讓本機版（webui.statusbar）與
# 靜態站（publish -> meta.json -> lists.html）用<b>同一份文字</b>。
# 之前這段是寫死在 server.py 的路由裡，靜態站就只能各抄一份 ——
# 抄了之後兩邊會各自漂移，而漂移的那一邊沒有人會發現。
NOTES = {}

for _k in ['score']:
    NOTES[_k] = (
        "bad", "<b>這份名單已驗證為反指標，不要照著買。</b>",
        "用 2019–2026 共 1,869 個交易日回測：<b>分數越高的股票，後續表現越差</b>"
        "（十分位單調性 −0.61，t = −4.44，扣成本後年化 −32%）。"
        "這不是「效果不明顯」，是方向相反。<br><br>"
        "放在這裡是因為藏起來不代表它不存在。它目前唯一誠實的用途是提醒你："
        "<b>這種「AI 選股名單」看起來多有說服力，實際上可能完全是反的</b>。<br><br>"
        "問題出在因子選擇，不是資料或計算 —— 計算層有 29 項對照已知答案的驗證。"
    )

for _k in ['low']:
    NOTES[_k] = (
        "bad", "<b>這份名單實測為反指標</b>，跌破 20 日低之後的表現顯著落後市場。",
        "用 148 個非重疊日期實測：取這份榜單的前 40 檔持有 10 日，"
        "<b>相對市場超額 −0.39%（t = −2.89）</b> —— "
        "在所有事實型榜單裡是唯一顯著落後的。<br><br>"
        "這跟個股頁測到的「跌破前 20 日低」型態一致（該型態的資訊量 −1.29%，"
        "是六種 K 線型態裡唯一超過交易成本的，但方向是負的）。<br><br>"
        "放在這裡是因為<b>「哪些不要碰」本身就是有用的資訊</b>，"
        "而且藏起來不代表它不存在。要靠它獲利必須放空，"
        "但台股放空受限（平盤下不得放空、借券成本、回補風險），實務上很難執行。"
    )

for _k in ['sue']:
    NOTES[_k] = (
        "ok", "這是本系統<b>第一個真正跨過交易成本</b>的選股訊號。",
        "PEAD（盈餘公布後漂移）是全球記錄最完整的異常之一：公布的數字優於預期之後，"
        "股價會在接下來數週持續往同方向漂移。這裡做的是它的月頻版本。<br><br>"
        "<b>先說清楚「預期」是什麼。</b>一般講「優於預期」多半指優於分析師預估，"
        "但本系統沒有分析師資料。這裡的基準是<b>公司自己過去 12 個月年增率的平均</b>，"
        "再除以同期間的標準差 —— 也就是「這次比自己的常軌好了幾個標準差」。"
        "除以自身波動，是為了讓「營收本來就穩定的公司出現落差」比"
        "「營收本來就大起大落的公司出現同樣落差」更有份量。<br><br>"
        "<b>跟「月營收年增」榜的差別很重要。</b>年增率問的是「成長得快不快」，"
        "這份榜單問的是「<b>比自己的常態好多少</b>」。一家連續三年年增 60% 的公司，"
        "這個月公布 40%，在年增率排行上仍然名列前茅 —— 但那其實是低於自己的常態，"
        "實測顯示這種股票接下來會弱。<br><br>"
        "<b>實測（71 個營收月份，以月份為觀察單位，做多最高 20% 這組）：</b><br>"
        "&nbsp;&nbsp;持有 10 日　+0.47%（t = 3.08）<br>"
        "&nbsp;&nbsp;持有 20 日　+1.16%（t = 5.75）<br>"
        "&nbsp;&nbsp;持有 40 日　+2.30%（t = 5.67）<br>"
        "&nbsp;&nbsp;持有 60 日　+3.66%（t = 4.91）<br><br>"
        "五分組<b>完全單調</b>（最低組 40 日 −1.76%，最高組 +2.30%），"
        "而且超額隨持有期<b>累積</b> —— 那正是 PEAD 的典型形狀，雜訊做不出來。"
        "前後兩半都成立，<b>八年逐年全部為正</b>，包括 2022 空頭年（+2.57%）——"
        "而那正是本系統其他訊號失效的時候。<br><br>"
        "<b>怎麼用。</b>這是事件驅動，訊號會衰減，所以榜單只留公布 45 天內的。"
        "適合的持有期是 <b>40-60 個交易日</b>，不是隔日沖 —— "
        "10 日的 +0.47% 扣掉 0.6% 成本就沒了，40 日的 +2.30% 才有空間。<br><br>"
        "<b>仍要注意</b>：樣本只有 71 個月（2019-06 起），而且月營收資料只涵蓋"
        "流動性最好的約 570 檔。這是本系統最強的發現，但不代表它不會失效。")

for _k in ['pos', 'rev']:
    NOTES[_k] = (
        "ok", "這是<b>唯二通過多重檢定校正</b>的兩份榜單。",
        "把畫面上 15 種排序方式全部用同一把尺量過（148 個非重疊日期，"
        "取前 40 檔持有 10 日，減掉當日全市場平均）：<br><br>"
        "<b>52 週高點接近度 +0.78%（t = 3.79）</b> —— 唯一超過 0.6% 來回成本的。<br>"
        "<b>月營收年增 +0.48%（t = 3.28）</b> —— 方向確定，但仍小於成本。<br><br>"
        "15 次檢定的 Bonferroni 門檻是 |t| &gt; 2.94，只有這兩個過關。"
        "成交額（t = 2.55）、法人買超（t = 2.23）、跌幅（t = 2.07）看起來有效，"
        "但在校正後都不算數。<br><br>"
        "<b>動能這一格特別容易踩錯。</b>文獻指出台股是動能效應的著名例外，"
        "而我們實測也確認了 —— 但要看是<b>哪一種</b>動能：<br>"
        "&nbsp;&nbsp;12-1 動能（經典 Jegadeesh-Titman）　10 日 +0.1%（t = 0.6）<b>無效</b><br>"
        "&nbsp;&nbsp;52 週高點接近度　　　　　　　　　　10 日 +0.6%（t = 3.5）<b>有效</b><br>"
        "&nbsp;&nbsp;3 個月動能　　　　　　　　　　　　t = 2.2，未達門檻<br>"
        "&nbsp;&nbsp;1 個月反轉　　　　　　　　　　　　t = −0.8，無訊號<br><br>"
        "這兩個常被混為一談，但在台股是一個有效一個無效。"
        "所以這份榜單用的是前者，不是「漲最多」。<br><br>"
        "<b>為什麼沒有高殖利率榜</b>：殖利率是全系統 IC 最高的訊號，"
        "但實測取殖利率最高的前 40 檔，超額是 <b>−0.10%（t = −0.60）</b>。"
        "它的資訊全在「排除最低的那一端」，不在「買最高的那一端」—— "
        "排除有效不等於選擇有效。")

for _k in ['screen']:
    NOTES[_k] = (
        "ok", "這兩條排除規則是系統中<b>唯一通過多重檢定校正</b>的發現。",
        "<b>規則一：排除殖利率最低 40%</b> —— 剩餘池子的<b>中位數</b>報酬 "
        "+5.49%/年（t = 4.69，p &lt; 0.0001）。注意是中位數不是平均數："
        "低殖利率股贏的次數少但偶爾大贏，平均被肥尾拉平，"
        "但你不會持有夠多檔去捕捉那條尾巴。<br><br>"
        "<b>規則二：排除價格位置最低 30%</b> —— 剩餘池子的<b>平均</b>報酬 "
        "+2.93%/年（t = 2.96）。四段樣本全部同向，5/10/20 日持有都成立，"
        "對應金融學的 52 週高點動能異常（George &amp; Hwang, 2004）。<br><br>"
        "<b>為什麼排除比選擇容易</b>：選擇要先跨過 0.6% 的來回交易成本，"
        "排除的成本門檻是 0 —— 不買某檔不用付任何錢。<br><br>"
        "<b>但這不是買進清單。</b>它只告訴你「這些沒被刷掉」，"
        "接下來該做的功課一樣都不能少。"
    )

for _k in ['inst', 'instout']:
    NOTES[_k] = (
        "warn", "法人買超是<b>目前唯一通過驗證的真訊號</b>，但效果小於交易成本。",
        "十分位剖面單調遞增（單調性 +0.88），樣本外毛價差仍為正（+0.23%/5 日）——"
        "這在本系統測過的所有訊號裡是獨一無二的，其他不是雜訊就是反指標。<br><br>"
        "<b>但是</b>：5 日換倉的來回成本約 0.97%，而訊號強度只有 0.23%。"
        "扣完成本年化 −32%。<br><br>"
        "所以它的正確用途是<b>當作參考資訊</b>（「今天法人在買這檔」是真的有意義的事實），"
        "而不是拿來當進出訊號。"
    )
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
