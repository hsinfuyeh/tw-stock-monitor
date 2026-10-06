"""影子名單：新的排除條件跟現在的名單並行測試，不取代它。

為什麼要有影子名單 ——

    第 11 輪的規範：名單的規則一改，前瞻實測（名單成績）就要重新起算。可是之後的研究
    （第 12 輪起：重大訊息、董監質押這類「排雷」條件）每一個都想知道「加上去會不會更好」。
    如果每試一個就改名單、實測就歸零，250 天永遠走不完。

    所以新條件不改名單，而是多存一份「套用新條件的名單」，兩份每天一起對答案：
      - 回測：同一天、同一個穩定池，名單 vs 影子名單逐日配對（大盤好壞兩份一起受，互相抵銷）
      - 前瞻：從登記那天起，影子名單每天存快照（snapshots/shadow/<key>/），10 個交易日後對答案
    原本的名單與它的實測完全不受影響。

怎麼加一份影子名單 ——

    在 VARIANTS 加一筆（事前登記：寫進 RESEARCH_LOG 之後才加，加了就不改）：
        key         英數字，快照資料夾名稱
        name / desc 網頁上顯示的名稱與白話說明
        registered  登記日（YYYY-MM-DD）。前瞻實測從這天起算，之前的日子不補（那是回測，不是前瞻）
        history_from  這個條件的資料從哪天起有歷史（回測從這天開始；沒歷史就填 None，只做前瞻）
        mask        函式：吃特徵面板的列，回傳布林 Series（True = 刷掉）。只能用當天收盤已知的資料

    名單規則（排序、檔數、同產業上限）跟原名單一模一樣，差別只有多刷掉的那些股票。

本機預覽：設 SHADOW_DEMO=1 會多一份「示範」影子名單（排除 60 日漲幅在穩定池前 20% 的股票），
只為了讓網頁有東西可以看。示範的快照只在記憶體裡算、不存檔，也不會出現在正式網站上。
"""
import json
import os

import numpy as np
import pandas as pd

import stable

ROOT = stable.ROOT
SHADOW = ROOT / "snapshots" / "shadow"

# 正式登記的影子名單。第 12 輪事前登記之後才會有第一筆。
VARIANTS = []


def _demo_mask(d):
    """示範用：60 日漲幅在當天穩定池前 20% 的刷掉（只為了本機預覽，沒有任何研究依據）。"""
    pool = stable.pool_mask(d)
    r = d["ret60"].where(pool).groupby(d["date"]).rank(pct=True)
    return (r > 0.8).fillna(False)


DEMO = dict(key="demo", name="示範：刷掉 60 日漲最多的兩成", demo=True,
            desc="只為了在本機預覽影子名單的版面，沒有任何研究依據，不會上正式網站。",
            registered=None, history_from="2008-01-01", mask=_demo_mask)


def variants():
    vs = list(VARIANTS)
    if os.environ.get("SHADOW_DEMO") and not os.environ.get("GITHUB_ACTIONS"):
        vs.append(DEMO)
    return vs


def snap_dir(v):
    return SHADOW / v["key"]


# --------------------------------------------------------------------- 回測（逐日配對）
def backtest_pair(d, lab, v, cfg=stable.CFG):
    """原名單 vs 影子名單，同一天配對。回傳 (逐日表, 摘要)；沒有歷史的條件回傳 (None, None)。"""
    if not v.get("history_from"):
        return None, None
    cal = np.sort(d["date"].unique())
    m = (d["date"] >= pd.Timestamp(v["history_from"])) & (d["date"] <= cal[-(cfg["hold"] + 1)])
    pool0 = stable.pool_mask(d, cfg) & m
    pool1 = pool0 & ~v["mask"](d).reindex(d.index).fillna(False).astype(bool)
    dates = d["date"]
    out = {}
    for key, pool in (("main", pool0), ("shadow", pool1)):
        _, sc = stable.score(d, pool)
        pk = stable.pick(d, sc, cfg)
        out[key] = stable._daily(lab, pk.index, dates)
    day = pd.DataFrame({"hit0": out["main"]["hit"], "fail0": out["main"]["fail"],
                        "hit1": out["shadow"]["hit"], "fail1": out["shadow"]["fail"],
                        "n1": out["shadow"]["n"]}).dropna(subset=["hit0", "hit1"])
    day["diff"] = (day["hit1"] - day["fail1"]) - (day["hit0"] - day["fail0"])
    return day, summarize_pair(day, cfg)


def _pair_sum(x, hold):
    if not len(x):
        return None
    # 影子名單跟原名單一模一樣的日子，差是 0；算進平均（那就是這個條件的真實效果），但另外報有差的天數
    return {"days": int(len(x)), "start": str(x.index.min())[:10], "end": str(x.index.max())[:10],
            "hit0": stable._p(x["hit0"]), "fail0": stable._p(x["fail0"]),
            "hit1": stable._p(x["hit1"]), "fail1": stable._p(x["fail1"]),
            "n1": stable._r(x["n1"].mean(), 1),
            "changed_days": int((x["diff"].abs() > 1e-12).sum()),
            "diff": stable._p(x["diff"]), "t": stable._nw(x["diff"], hold)}


def summarize_pair(day, cfg=stable.CFG):
    h = cfg["hold"]
    per = {name: _pair_sum(day[(day.index >= a) & (day.index <= b)], h) for name, a, b in stable.PERIODS}
    allp = _pair_sum(day[(day.index >= "2008-01-01") & (day.index <= "2025-12-31")], h)
    return {"periods": per, "all": allp}


# --------------------------------------------------------------------- 前瞻（快照）
def snapshot(d, ds, risk, v, backfilled=False):
    rows, _ = stable.today_list(d, ds, risk, extra=v["mask"])
    return {"date": ds, "variant": v["key"], "backfilled": backfilled,
            "rows": [{k: r[k] for k in ("rank", "code", "name", "ind", "score", "close", "why")} for r in rows],
            "risk_status": {k: (risk or {}).get(k) for k in ("disposal_status", "notice_status", "full_status")}}


def forward_snaps(d, risk, v, risk_for=None, main_snaps=None, max_days=10):
    """這份影子名單的快照：已存的 + 今天的 + 登記日之後沒人更新而缺的（補算）。
    回傳 (全部快照, 這次要寫進檔案的快照)。示範版不存檔，從原名單的第一份快照起在記憶體裡算。"""
    cal = sorted({str(x)[:10] for x in d["date"].unique()})
    today = cal[-1]
    if v.get("demo"):
        start = min(main_snaps) if main_snaps else today
        snaps = {ds: snapshot(d, ds, risk if ds == today else None, v, backfilled=ds != today)
                 for ds in cal if ds >= start}
        return snaps, []
    snaps = stable.load_snaps(snap_dir(v))
    new = {today: snapshot(d, today, risk, v)}
    start = v["registered"]
    missing = [x for x in cal[:-1] if x >= start and x not in snaps][-max_days:]
    for ds in missing:
        new[ds] = snapshot(d, ds, risk_for(ds) if risk_for else None, v, backfilled=True)
    snaps.update(new)
    return snaps, list(new.values())


def pair_forward(main_days, shadow_days):
    """前瞻的逐日配對：兩份都對完答案的日子，影子的（中 − 倒）減原名單的。"""
    m = {x["date"]: x for x in main_days if x.get("diff") is not None}
    diffs = []
    for x in shadow_days:
        y = m.get(x["date"])
        if x.get("diff") is None or y is None or not x["done"] or not y["done"]:
            continue
        diffs.append((x["hit"] - x["fail"]) / x["done"] - (y["hit"] - y["fail"]) / y["done"])
    diffs = np.array(diffs, float)
    return {"days": int(len(diffs)), "diff": stable._r(diffs.mean() * 100, 1) if len(diffs) else None,
            "t": stable._nw(diffs, stable.CFG["hold"])}


# --------------------------------------------------------------------- 產出
def compute(d, risk, main_days, main_snaps=None, risk_for=None, lab=None):
    """shadow.json 的內容，以及這次要寫進 snapshots/shadow/ 的快照。沒有影子名單就回傳空的。"""
    vs = variants()
    if not vs:
        return {"variants": []}, []
    lab = stable.labels(d) if lab is None else lab
    out, writes = [], []
    for v in vs:
        day, bt = backtest_pair(d, lab, v)
        snaps, new = forward_snaps(d, risk, v, risk_for, main_snaps)
        if not v.get("demo"):
            writes += [(v, s) for s in new]
        tdays, tsum = stable.track(d, lab, snaps) if snaps else ([], None)
        out.append({"key": v["key"], "name": v["name"], "desc": v["desc"], "demo": bool(v.get("demo")),
                    "registered": v.get("registered"), "history_from": v.get("history_from"),
                    "backtest": bt,
                    "recent": [{"date": str(i)[:10], "diff": stable._r(r["diff"] * 100, 1)}
                               for i, r in day.tail(60).iloc[::-1].iterrows()] if day is not None else [],
                    "today": (snaps.get(max(snaps)) or {}).get("rows", []) if snaps else [],
                    "track": tsum, "days": tdays[:60], "vs_main": pair_forward(main_days, tdays)})
    return {"variants": out}, writes


def write(out_dir, shadow_json, writes):
    (out_dir / "stable").mkdir(parents=True, exist_ok=True)
    (out_dir / "stable" / "shadow.json").write_text(stable._json(shadow_json), encoding="utf-8")
    if stable.persist() and writes:
        import shortterm
        for v, s in writes:
            p = snap_dir(v)
            p.mkdir(parents=True, exist_ok=True)
            (p / (s["date"] + ".json")).write_text(
                json.dumps(shortterm._json_safe(s), ensure_ascii=False, indent=1), encoding="utf-8")
