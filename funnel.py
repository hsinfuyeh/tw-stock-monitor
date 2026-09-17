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
    ("L0", "可交易宇宙", "上市普通股", "fact",
     "目前只涵蓋上市。上櫃 700+ 檔需接 TPEx 資料源。"),
    ("L1", "流動性不足", "20 日均額 < 2,000 萬", "fact",
     "全市場約 26% 的證券日成交量低到實際無法交易，"
     "那些報價是買賣價差跳動而非真實成交。這是執行面事實，不需統計驗證。"),
    ("L1b", "當天鎖漲跌停", "開高低收同價且觸及漲跌停", "fact",
     "鎖死時買不到也賣不掉。實測全市場 3.17% 的交易日收在漲停、"
     "1.10% 收在跌停，其中完全鎖死的日子實務上無法成交。"),
    ("L2a", "殖利率位階過低", "全市場最低 40%", "validated",
     "排除後剩餘池的<b>中位數</b>報酬 +5.49%/年（t = 4.69，p &lt; 0.0001，"
     "通過 Bonferroni 校正）。注意是中位數不是平均數 —— "
     "低殖利率股贏的次數少但偶爾大贏，平均被肥尾拉平。"),
    ("L2b", "價格位置過低", "近一年區間最低 30%", "validated",
     "排除後剩餘池的<b>平均</b>報酬 +2.93%/年（t = 2.96）。"
     "四段樣本全部同向，5/10/20 日持有都成立，"
     "對應金融學的 52 週高點動能異常（George &amp; Hwang, 2004）。"),
    ("L3", "營收衰退", "月營收年增率 < 0", "weak",
     "<b>用途是分辨「便宜的好公司」與「價值陷阱」。</b>"
     "殖利率 = 股利 ÷ 股價，股價暴跌會讓殖利率飆高 —— "
     "統計層會把它評為高分，但那不是便宜，是公司在爛掉。<br><br>"
     "<b>實測結果（142 個非重疊日期，在候選池<i>內部</i>比較）：</b>"
     "營收成長組未來 10 日平均 +1.00%、中位 +0.30%、勝率 50.5%；"
     "營收衰退組 +0.75%、+0.07%、48.2%。"
     "差距 +0.25%（t = 2.00），中位數差 +0.23%（t = 2.21）。<br><br>"
     "把池子依「殖利率高低 × 營收增減」切四格，<b>四格方向全部一致</b>，"
     "而且效果在高殖利率那半邊更大（+0.31%，t = 2.26）比低殖利率那半邊"
     "（+0.17%，t = 1.06）強 —— 這正是價值陷阱說法預測的形狀。<br><br>"
     "<b>但強度只到邊緣。</b>t 約 2.0–2.3，達不到 L2a／L2b 那種"
     "通過多重檢定校正的水準。所以它留著，但不該跟上面兩層當成同一等級的證據。"
     "留著的理由是<b>排除的成本門檻是 0</b> —— 不買某檔不用付任何錢，"
     "所以只要方向是對的就值得排除；選擇才需要先跨過 0.6% 的手續費。"),
]

EV_LABEL = {"fact": "執行面事實", "validated": "已驗證",
            "weak": "方向一致但弱", "pending": "資料回補中", "untested": "未驗證"}


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
                          "營收年增 {:+.0f}%，法人卻買超。可能是預期反轉，"
                          "也可能只是造市。".format(yoy)))
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
                          "<b>價值陷阱的典型特徵</b>：高殖利率可能來自股價下跌，"
                          "而非公司便宜。".format(dy, yoy)))
    return flags
