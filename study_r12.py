"""第 12 輪：重大負面公告排雷（RESEARCH_LOG 2026-10-08 事前登記）。

照登記跑，不改任何定義：
    主假設  影子名單 = 第 11 輪名單 + 刷掉近 10 個交易日內有重大負面公告（五類）的股票
    主指標  每天 影子（中 − 倒）− 原名單（中 − 倒），NW t lag 10
    判定    2008–2018、2019–2025 都 > 0，且 2008–2025 t ≥ 2；名單有變的天數 < 100 -> 樣本不足

跑之前先檢查重大訊息是不是 2008-01-01 到今天每一天都抓到了 —— 沒抓齊就不跑（半套的資料會少算事件，
讓影子名單跟原名單看起來比實際更像，結果偏向「沒差」）。

    python study_r12.py
"""
import datetime as dt
import sys

import numpy as np
import pandas as pd

import events
import mops_news
import shadow
import stable
import store


def check_complete():
    have = {p.name[:8] for p in mops_news.DIR.glob("*.json.gz")}
    d0, d1 = mops_news.START, dt.date.today() - dt.timedelta(days=1)
    miss = [d for d in (d0 + dt.timedelta(days=i) for i in range((d1 - d0).days + 1))
            if d.strftime("%Y%m%d") not in have]
    return miss


def variant(tagged, window, cats=None):
    ev = tagged if cats is None else tagged[tagged["cats"].map(lambda c: bool(set(c) & set(cats)))]
    return dict(key="r12_w{}".format(window), name="排雷 {} 天".format(window), desc="", registered=None,
                history_from="2008-01-01", mask=lambda d: events.recent_flag(d, ev, window))


def row(name, x):
    if not x:
        return "| {} | — | — | — | — | — |".format(name)
    return "| {} | {}／{} | {}／{} | {} | **{:+.2f}**（t {}） | {} |".format(
        name, x["hit0"], x["fail0"], x["hit1"], x["fail1"], x["changed_days"], x["diff"],
        x["t"] if x["t"] is not None else "—", x["days"])


def table(title, bt):
    out = ["", "#### " + title, "",
           "| 期間 | 原名單 中／倒 | 影子 中／倒 | 名單有變的天數 | **主指標**（影子 − 原，百分點） | 訊號日 |",
           "|---|---|---|---|---|---|"]
    for name in ("2008–2018", "2019–2025", "2026"):
        out.append(row(name + ("（只看一次）" if name == "2026" else ""), bt["periods"].get(name)))
    out.append(row("**2008–2025**", bt["all"]))
    return out


def direct(d, lab, flag, cfg=stable.CFG):
    """原名單裡「近 10 天有負面公告」的股票本身的中／倒，跟同一天原名單其他股票比；以及穩定池裡有／沒有公告的比較。"""
    cal = np.sort(d["date"].unique())
    m = (d["date"] <= cal[-(cfg["hold"] + 1)]) & (d["date"] <= pd.Timestamp("2025-12-31"))
    pool = stable.pool_mask(d, cfg) & m
    _, sc = stable.score(d, pool)
    pk = stable.pick(d, sc, cfg)
    L = lab["label"]
    sel = pk.index[L[pk.index].notna()]
    f = flag[sel]
    res = {}
    for name, idx in (("名單裡有公告的", sel[f.to_numpy()]), ("名單裡其他的", sel[~f.to_numpy()])):
        y = L[idx]
        res[name] = dict(n=int(len(y)), hit=round(float((y == 1).mean() * 100), 1) if len(y) else None,
                         fail=round(float((y == -1).mean() * 100), 1) if len(y) else None)
    pidx = pool[pool].index
    pidx = pidx[L[pidx].notna()]
    for name, idx in (("穩定池裡有公告的", pidx[flag[pidx].to_numpy()]), ("穩定池裡沒有的", pidx[~flag[pidx].to_numpy()])):
        y = L[idx]
        res[name] = dict(n=int(len(y)), hit=round(float((y == 1).mean() * 100), 1) if len(y) else None,
                         fail=round(float((y == -1).mean() * 100), 1) if len(y) else None)
    return res


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    miss = check_complete()
    if miss and "--selftest" not in sys.argv:
        print("重大訊息還沒抓齊：缺 {} 天（例如 {}）。先跑 python mops_news.py。".format(
            len(miss), ", ".join(str(x) for x in miss[:5])))
        return 1
    mops_news.build()
    news = store.q("SELECT * FROM mops_news")
    d = stable.features()
    cal = pd.DatetimeIndex(sorted(d["date"].unique()))
    tagged = events.tag(news, cal)
    if "--selftest" in sys.argv:
        # 只測流程：把每則公告隨機換到別家公司，結果沒有意義（不會看到真正的答案）
        rng = np.random.default_rng(0)
        codes = d["code"].astype(str).unique()
        tagged["code"] = rng.choice(codes, size=len(tagged))
        print("=== 自我測試：公告的公司已隨機打亂，以下數字沒有意義 ===")
    lab = stable.labels(d)
    print("公告 {:,} 則，有分類的 {:,} 則，其中重大負面 {:,} 則".format(len(news), len(tagged), int(tagged["neg"].sum())))
    lines = ["### 第 12 輪結果（{}，照事前登記跑，沒有改任何定義）".format(dt.date.today()), "",
             "程式 `study_r12.py`。重大訊息 {:,} 則，分類後重大負面 {:,} 則（{} ~ {}）。".format(
                 len(news), int(tagged["neg"].sum()), str(news["ts"].min())[:10], str(news["ts"].max())[:10])]
    prim = None
    for title, w, cats in (("主假設：五類、近 10 個交易日", 10, None),
                           ("敏感度：五類、近 5 個交易日", 5, None),
                           ("敏感度：五類、近 20 個交易日", 20, None),
                           ("敏感度：只用司法調查／財務危機／會計異常、近 10 個交易日", 10, ["legal", "distress", "audit"])):
        _, bt = shadow.backtest_pair(d, lab, variant(tagged, w, cats))
        lines += table(title, bt)
        print(title, bt["all"])
        if prim is None:
            prim = bt
    a, b, al = prim["periods"].get("2008–2018"), prim["periods"].get("2019–2025"), prim["all"]
    if al["changed_days"] < 100:
        verdict = "**樣本不足**：2008–2025 名單有變的天數只有 {} 天（< 100），不判定。".format(al["changed_days"])
    elif a and b and a["diff"] > 0 and b["diff"] > 0 and (al["t"] or 0) >= 2:
        verdict = "**回測支持**（兩段 > 0、全期 t {} ≥ 2）。照登記，登記成影子名單開始前瞻實測。".format(al["t"])
    else:
        verdict = "**回測不支持**（兩段 {:+.2f}／{:+.2f}、全期 t {}）。照登記，不登記影子名單；公司事件只在網頁顯示。".format(
            a["diff"] if a else float("nan"), b["diff"] if b else float("nan"), al["t"])
    lines += ["", "**判定**：" + verdict, "", "**直接證據**（2008–2025，近 10 天有五類負面公告）：", "",
              "| 群組 | 筆數 | 中 | 倒 |", "|---|---|---|---|"]
    flag = events.recent_flag(d, tagged, 10)
    for k, v in direct(d, lab, flag).items():
        lines.append("| {} | {:,} | {} | {} |".format(k, v["n"], v["hit"], v["fail"]))
    txt = "\n".join(lines)
    print(txt)
    return txt


if __name__ == "__main__":
    out = main()
    if isinstance(out, int):
        sys.exit(out)
