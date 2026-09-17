"""降低成本門檻的實驗。

背景：法人買超是唯一通過驗證的真訊號（單調性 +0.88、樣本外毛價差 +0.23%/5日），
但 5 日換倉 × 80% 換手的成本是 0.96%，訊號被成本吃掉。

改進的方向不只有「找更強的訊號」，還有「讓同樣的訊號付更少成本」：

  1. 緩衝帶（no-trade band）—— 進場用嚴格門檻、出場用寬鬆門檻。
     部位只有跌出寬門檻才換掉，不是每期重排。這是最標準的降換手做法。
  2. 只做多 —— 成本直接砍半（一條腿而非兩條），
     而且台股放空本來就受限（平盤下不得放空、借券成本），多空組合實務上難執行。
  3. 拉長持有 —— 但訊號會衰減，要實測衰減速度。
"""
import numpy as np
import pandas as pd

from config import COST_ROUND_TRIP as COST


def buffered_backtest(panel, factor, horizon=5, enter_pct=0.10, exit_pct=0.30,
                      long_only=True, cost=COST):
    """帶緩衝帶的回測。

    enter_pct=0.10 / exit_pct=0.30 的意思是：
      分數排進前 10% 才買進，但要跌出前 30% 才賣出。
      中間那段 10~30% 就是緩衝帶，在裡面的部位不動 -> 換手大幅下降。
    """
    fw = "fwd{}".format(horizon)
    d = panel[["date", "code", factor, fw]].dropna().copy()
    d[fw] = d[fw] - d.groupby("date")[fw].transform("mean")   # 市場中性
    dates = sorted(d["date"].unique())[::horizon]
    d = d[d["date"].isin(dates)]
    d["pct"] = d.groupby("date")[factor].rank(pct=True)

    held_l, held_s = set(), set()
    recs = []
    for dt_ in dates:
        day = d[d["date"] == dt_]
        if len(day) < 100:
            continue
        rank = dict(zip(day["code"], day["pct"]))
        ret = dict(zip(day["code"], day[fw]))

        # 多方：留下仍在 exit 門檻內的，補進新的高分股
        keep_l = {c for c in held_l if rank.get(c, 0) >= 1 - exit_pct}
        cand_l = [c for c in day.sort_values(factor, ascending=False)["code"]
                  if c not in keep_l]
        target = max(1, int(len(day) * enter_pct))
        new_l = set(list(keep_l) + cand_l[:max(0, target - len(keep_l))])

        turn_l = len(new_l - held_l) / max(len(new_l), 1) if held_l else 1.0
        r_l = np.mean([ret[c] for c in new_l if c in ret]) if new_l else 0.0

        if long_only:
            gross = r_l
            turn = turn_l
            legs = 1
        else:
            keep_s = {c for c in held_s if rank.get(c, 1) <= exit_pct}
            cand_s = [c for c in day.sort_values(factor)["code"] if c not in keep_s]
            new_s = set(list(keep_s) + cand_s[:max(0, target - len(keep_s))])
            turn_s = len(new_s - held_s) / max(len(new_s), 1) if held_s else 1.0
            r_s = np.mean([ret[c] for c in new_s if c in ret]) if new_s else 0.0
            gross = r_l - r_s
            turn = (turn_l + turn_s) / 2
            legs = 2
            held_s = new_s
        held_l = new_l

        recs.append(dict(date=dt_, gross=gross, turnover=turn,
                         net=gross - cost * 100 * turn * legs))

    if len(recs) < 20:
        return None
    r = pd.DataFrame(recs).iloc[1:]      # 第一期建倉換手必為 100%，剔除
    per_year = 252 / horizon
    cum = (1 + r["net"] / 100).cumprod()
    return dict(
        設定="{}／進{:.0%}出{:.0%}".format("只做多" if long_only else "多空",
                                          enter_pct, exit_pct),
        期數=len(r),
        毛價差=round(float(r["gross"].mean()), 3),
        換手率=round(float(r["turnover"].mean()) * 100, 1),
        淨價差=round(float(r["net"].mean()), 3),
        年化淨=round(float(r["net"].mean()) * per_year, 2),
        最大回撤=round(float((cum / cum.cummax() - 1).min() * 100), 1),
        勝率=round(float((r["net"] > 0).mean()) * 100, 1),
    )
