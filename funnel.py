"""層層剔除漏斗。

設計原則 —— 每一層都要回答三件事：
    刷掉幾檔？   為什麼刷？   這個理由有多少證據？

「證據強度」分四級，直接標在畫面上：
    fact       執行面事實，不需統計驗證（買不到就是買不到）
    validated  通過多重檢定校正的統計規則
    pending    資料還在回補，尚未驗證
    untested   有理論依據但本系統尚未驗證
"""
import numpy as np
import pandas as pd

# (代號, 名稱, 說明, 證據強度, 證據細節)
LAYERS = [
    ("L0", "起點", "全部上市普通股", "fact",
     "目前只涵蓋上市股票。上櫃股票還沒有接資料。"),
    ("L1", "成交量太小", "近 20 日平均每天成交不到 2,000 萬", "fact",
     "全市場約四分之一的股票成交量小到實際上買不到、賣不掉，"
     "看到的價格常常只是零星成交。這是交易實務，不需要統計證明。"),
    ("L1b", "整天鎖漲跌停", "開盤、最高、最低、收盤都一樣且碰到漲跌停", "fact",
     "整天鎖在漲停或跌停，想買買不到、想賣賣不掉。"),
    ("L2a", "殖利率偏低", "殖利率在全市場最低的 40%", "validated",
     "過去統計，刷掉這一群之後，剩下的股票一般情況下（中位數）每年多賺約 5.5%，"
     "而且差距大到不像巧合。<br><br>"
     "注意是「一般情況」不是「平均」：低殖利率的股票大多時候表現普通，"
     "偶爾有一兩檔大漲把平均拉高 —— 但你很難剛好買到那一兩檔。"),
    ("L2b", "股價在一年低檔", "在一年高低區間的最低 30%", "validated",
     "過去統計，刷掉這一群之後，剩下的股票平均每年多賺約 2.9%。"
     "把 7 年資料切成四段分別看，四段方向都一樣；持有 5、10、20 天也都成立。"),
    ("L3", "營收衰退", "月營收比去年同月少", "weak",
     "<b>用來分辨「便宜的好公司」和「看起來便宜、其實在走下坡的公司」。</b>"
     "殖利率 = 股利 ÷ 股價，股價大跌會讓殖利率變高 —— 那不是便宜，是公司在變差。"
     "<br><br><b>過去統計（在已經通過前面幾層的股票裡比較）：</b>"
     "營收成長的，之後 10 天平均 +1.00%；營收衰退的 +0.75%。差 0.25%。<br><br>"
     "<b>但證據偏弱</b>：差距不大，有可能是巧合，不能跟上面兩層當成同一等級。"
     "還是留著，是因為<b>排除不用付錢</b> —— 不買一檔股票沒有手續費，"
     "所以只要方向是對的就值得刷掉。"),
]

EV_LABEL = {"fact": "交易實務", "validated": "有回測支持",
            "weak": "證據偏弱", "pending": "資料補齊中", "untested": "未測試"}


# 候選名單排序的說明。
ORDER_NOTE = (
    "warn",
    "這個順序<b>只代表建議先看哪幾檔，不是買進順序</b>。",
    "排序方式：把「在一年區間的位置」「月營收年增」「營收優於預期」三項的排名平均。"
    "<br><br>"
    "<b>為什麼這樣排。</b>在通過篩選的股票裡試了好幾種排法（過去 7 年、140 個互不重疊的日期，"
    "取前 30 檔持有 10 天，跟全部候選的平均比）：<br><br>"
    "<b>現在用的三項合併　平均多 0.58%</b><br>"
    "只看營收優於預期　多 0.56%<br>"
    "位置 + 營收年增　　多 0.45%<br>"
    "以前用的歷史勝率　多 0.13%（跟隨便挑差不多）<br>"
    "隨便挑 30 檔　　　−0.04%<br><br>"
    "把 7 年切成前後兩半，現在這個排法兩半都成立，不像巧合。<br><br>"
    "<b>但要注意</b>：0.58% 還<b>不夠付 0.6% 的手續費和稅</b>，"
    "所以不能拿來每 10 天換一次股票；它的用途是<b>決定你先研究哪幾檔</b>。"
    "想要扣掉成本還有剩，看「其他排行 → 營收優於預期」並抱 40–60 個交易日。")


def rank_candidates(d):
    """候選名單的排序分數。越大排越前面。

    為什麼不用「歷史勝率等級」—— 那是之前的做法，實測等於隨機（見 ORDER_NOTE）。
    等級是用殖利率位階與價格位置合成的，而這兩件事在池子裡<b>已經被當成排除
    條件用掉了</b>：通過的人在這兩項上都已經不差，再拿同樣的東西排序，
    自然分不出高下。

    現在用三個在池內仍有鑑別力的東西等權合成：價格位置、月營收年增、優於預期幅度。
    各自先做當日橫斷面標準化再平均，所以量綱不同不影響。

    為什麼不用「優於預期單獨」—— 它的 t 值其實最高（+0.56%，t = 5.35，
    比三者合成的 t = 4.77 還高）。不採用是因為那等於把全部賭在單一訊號上，
    而這個專案已經被這件事咬過（原本的「歷史勝率等級」就是單一來源，
    實測等於隨機）。而且 SUE 的覆蓋率只有 82%，另外兩個各有 97% 以上。
    三者合成在前 30 檔的幅度也較大（+0.58% vs +0.56%）。

    月營收缺值的（池內視年份約 14-31%）把營收那一項<b>當成中性 0</b>，
    不是「只用價格位置算」也不是「排到最後」。三種做法實測過：

        兩者皆需，缺值排除      +0.46%  t=3.78
        有什麼用什麼（只用價位） +0.32%  t=2.60   <- 明顯較差
        缺值視為中性 0          +0.45%  t=3.57

    「只用價格位置」直覺上比較體貼，實際上讓排序變差 —— 單一成分的雜訊
    比兩個成分大，硬拿去跟雙成分的分數比高下並不公平。
    「排到最後」則等於宣稱「沒有資料 = 營收很差」，那也不是事實
    （FinMind 沒收錄而已）。中性 0 是唯一誠實的處理，而且統計上不輸。
    """
    import factors as F
    zp = F.zscore_by_date(d["pos252"], d["date"])
    zr = F.zscore_by_date(d["yoy"], d["date"])
    zs = (F.zscore_by_date(d["sue"], d["date"]) if "sue" in d.columns
          else pd.Series(0.0, index=d.index))
    return (zp + zr.fillna(0) + zs.fillna(0)) / 3


def build(day, rev_col="yoy"):
    """對「最新交易日」跑一次漏斗，回傳每層的統計與最終存活名單。

    day: 單一交易日的橫斷面（已含 amt20 / locked / ypct / ppct，可能含 yoy）
    """
    steps = []
    alive = pd.Series(True, index=day.index)

    def cut(key, mask_out):
        """mask_out = True 代表要被刷掉。"""
        nonlocal alive
        before = int(alive.sum())
        removed = int((alive & mask_out).sum())
        alive = alive & ~mask_out
        meta = next(x for x in LAYERS if x[0] == key)
        steps.append(dict(key=key, name=meta[1], rule=meta[2],
                          evidence=meta[3], detail=meta[4],
                          before=before, removed=removed, after=int(alive.sum())))

    steps.append(dict(key="L0", name=LAYERS[0][1], rule=LAYERS[0][2],
                      evidence=LAYERS[0][3], detail=LAYERS[0][4],
                      before=len(day), removed=0, after=len(day)))

    cut("L1", day["amt20"].isna() | (day["amt20"] < 2e7))
    cut("L1b", day.get("locked", pd.Series(False, index=day.index)).fillna(False))
    cut("L2a", (day["ypct"] < 0.40).fillna(False))
    cut("L2b", (day["ppct"] < 0.30).fillna(False))

    if rev_col in day.columns and day[rev_col].notna().any():
        cov = float(day.loc[alive, rev_col].notna().mean()) if alive.any() else 0.0
        # 只對「有營收資料」的做判斷，沒資料的不刷掉（避免因缺資料誤殺）
        cut("L3", (day[rev_col] < 0).fillna(False))
        steps[-1]["coverage"] = cov
    else:
        meta = next(x for x in LAYERS if x[0] == "L3")
        steps.append(dict(key="L3", name=meta[1], rule=meta[2], evidence=meta[3],
                          detail=meta[4], before=int(alive.sum()), removed=0,
                          after=int(alive.sum()), coverage=0.0))

    return steps, day[alive]


def evidence_row(r):
    """一檔股票的多來源證據，以及來源之間是否互相矛盾。

    矛盾本身就是重要資訊：營收創高但法人在賣，代表有人知道你不知道的事。
    這裡只標記矛盾，不解讀 —— 解讀是使用者的工作。
    """
    flags = []
    yoy = r.get("yoy")
    inst = r.get("法人買超")
    chg = r.get("chg_pct")
    has_rev = yoy is not None and not pd.isna(yoy)
    has_inst = inst is not None and not pd.isna(inst)

    if has_rev and has_inst:
        if yoy > 10 and inst < -0.10:
            flags.append(("warn", "營收成長但法人賣超",
                          "營收年增 {:+.0f}%，但法人賣超相當於 {:.0f}% 日均量。"
                          "有人知道你不知道的事，值得查原因。".format(yoy, abs(inst) * 100)))
        elif yoy < -10 and inst > 0.10:
            flags.append(("warn", "營收衰退但法人買超",
                          "營收年增 {:+.0f}%，法人卻在買。可能是看好之後好轉，"
                          "也可能只是短線進出。".format(yoy)))
    if has_rev and chg is not None and not pd.isna(chg):
        if yoy > 20 and chg < -3:
            flags.append(("warn", "營收大增但今日重挫",
                          "營收年增 {:+.0f}% 但今天跌 {:.1f}% —— "
                          "可能有營收以外的利空。".format(yoy, abs(chg))))
    dy = r.get("div_yield")
    if has_rev and dy is not None and not pd.isna(dy):
        if dy > 5 and yoy < 0:
            flags.append(("bad", "高殖利率但營收衰退",
                          "殖利率 {:.1f}% 但營收年增 {:+.0f}% —— "
                          "<b>典型的「看起來便宜的陷阱」</b>：殖利率高可能是因為股價跌了，"
                          "不是公司真的便宜。".format(dy, yoy)))
    return flags
