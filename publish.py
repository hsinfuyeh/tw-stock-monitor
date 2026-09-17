"""產出靜態網站的資料檔。

為什麼要這一層 ——

    本機版把 1,873 天的完整面板常駐記憶體（實測 2 GB），因為研究需要：
    回測、IC、十分位剖面都得掃全歷史。但<b>網頁顯示</b>幾乎用不到那些：
    最新一日的橫斷面只有 551 檔 / 0.4 MB，個股頁只要自己那條序列。

    所以這裡把「研究」與「服務」切開。本機保留完整倉儲做驗證，
    這支程式只吐出顯示需要的東西 —— 實測整站約 8 MB，
    小到可以直接放 GitHub Pages，不需要伺服器、不需要資料庫。

刻意的取捨 ——

    所有數字一律<b>透過 server / screens / funnel 既有的函式產生</b>，
    不在這裡重寫一份計算邏輯。重寫等於製造第二個真相來源，
    兩邊遲早會不一致，而且不一致時沒人會發現（本機看起來對，網頁悄悄錯）。

    K 線圖直接輸出 SVG 字串而不是原始資料點。畫圖的程式碼在
    stock_report.candles_svg，移植到 JS 要重寫一次座標與刻度邏輯，
    那正是最容易出現「看起來很像但其實不同」的地方。

跑法：
    python publish.py            產出全部（1,475 檔，約 2-4 分鐘）
    python publish.py 20         只產前 20 檔，開發時用
"""
import datetime as dt
import gzip
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import factors
import funnel
import screens
import server
import stock_report
import webui
from config import COST_ROUND_TRIP

SITE = Path(__file__).parent / "site"
DATA = SITE / "data"


# --------------------------------------------------------------------- 工具
def _clean(o):
    """把 pandas / numpy 的型別換成 JSON 寫得出來的東西。

    NaN 一律轉 None 而不是留著 —— json.dumps 會把 NaN 寫成裸的 `NaN`，
    那不是合法 JSON，瀏覽器的 JSON.parse 會直接拋錯。
    """
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(x) for x in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if pd.isna(o) else round(float(o), 6)
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, dt.date, dt.datetime)):
        return str(o)[:10]
    if o is None or (isinstance(o, str) and o == ""):
        return o
    if isinstance(o, str):
        return o
    try:
        return None if pd.isna(o) else o
    except Exception:
        return str(o)


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(_clean(obj), ensure_ascii=False, separators=(",", ":"))
    path.write_text(txt, encoding="utf-8")
    return len(txt.encode())


def _row_dict(r, cols):
    return {c: r.get(c) for c in cols if c in r}


# --------------------------------------------------------------------- 各區塊
LIST_COLS = ["code", "name", "close", "chg_pct", "amt20", "div_yield", "pos252",
             "yoy", "法人買超", "tier", "vol_ratio", "sd60"]


def emit_meta(p, day_date):
    """資料日期與各層證據強度。前端的過期橫幅也靠這個。"""
    return dict(
        data_date=str(day_date)[:10],
        built_at=dt.datetime.now().isoformat(timespec="seconds"),
        cost_round_trip=COST_ROUND_TRIP,
        evidence_labels=funnel.EV_LABEL,
        layers=[dict(key=k, name=n, rule=r, evidence=e, detail=d)
                for k, n, r, e, d in funnel.LAYERS],
        screens=[dict(key=k, title=t, desc=d, cat=c,
                      tab=screens.TAB_LABEL.get(k, t))
                 for k, t, d, c in screens.SCREENS],
        # 分組與「訊號」歸類一起吐出去。原本靜態站自己抄了一份 GROUPS，
        # 結果新增營收榜單時只改到 server.py，靜態站的分頁就少一個 ——
        # 而且不會報錯，只是那個榜單在畫面上消失。
        tab_groups=[dict(label=g, keys=ks) for g, ks in server.TAB_GROUPS_SPEC],
        signal_keys=list(server.SIGNAL_KEYS),
        glossary=webui.GLOSSARY,
    )


def emit_universe():
    u = server.universe()
    return [dict(code=str(r["code"]), name=str(r["name"]),
                 kind=server._kind_of(r)) for _, r in u.iterrows()]


def emit_funnel(pfull):
    day, dt_ = screens._latest(pfull)
    prev = screens._prev_close(pfull, dt_)
    day["prev"] = day["code"].map(prev)
    day["chg_pct"] = (day["close"] / day["prev"] - 1) * 100
    steps, alive = funnel.build(day)
    alive = alive.assign(_rank=funnel.rank_candidates(alive))
    alive = alive.sort_values(["_rank", "amt20"], ascending=False)

    rows = []
    for _, r in alive.iterrows():
        d = _row_dict(r, LIST_COLS)
        d["code"] = str(r["code"])
        d["name"] = str(r["name"])
        # 來源之間的矛盾。只標記不解讀 —— 解讀是使用者的工作。
        d["flags"] = [dict(level=c, title=t, detail=x)
                      for c, t, x in funnel.evidence_row(r)]
        rows.append(d)
    return dict(date=str(dt_)[:10], steps=steps, total=steps[0]["after"],
                passed=steps[-1]["after"], rows=rows,
                order_note=funnel.ORDER_NOTE)


def emit_screens(p):
    out = {}
    for key, title, desc, cat in screens.SCREENS:
        if key == "screen":
            continue          # 已改為導向選股頁，不再單獨產出
        try:
            if key == "exdiv":
                df = screens.upcoming_exdiv(p)
                rows = [dict(code=str(r["code"]), name=str(r["name"]),
                             date=str(r["date"])[:10],
                             days_left=int(r["days_left"]),
                             value=float(r["value"]),
                             yield_pct=float(r["yield_pct"]))
                        for _, r in df.iterrows()]
                out[key] = dict(key=key, title=title, desc=desc, cat=cat,
                                extra=None, extra_title=None,
                                note=screens.NOTES.get(key), rows=rows)
                continue
            if key == "score":
                # run_screen 沒有 score 分支（會回傳空表），要走專用函式。
                # 上一版就是漏了這個，靜態站產出一份 0 筆的綜合評分頁，
                # 而且完全沒有錯誤訊息。
                top, bot = screens.score_rows(p)
                def _pack(df):
                    rr = []
                    for _, r in df.iterrows():
                        d = _row_dict(r, LIST_COLS)
                        d["code"] = str(r["code"]); d["name"] = str(r["name"])
                        d["extra"] = r.get("_s")
                        rr.append(d)
                    return rr
                out[key] = dict(key=key, title=title, desc=desc, cat=cat,
                                extra="_s", extra_title="綜合分數",
                                note=screens.NOTES.get(key),
                                rows=_pack(top), rows_bottom=_pack(bot))
                continue
            df, ek, et = screens.run_screen(p, key)
            rows = []
            for _, r in df.iterrows():
                d = _row_dict(r, LIST_COLS)
                d["code"] = str(r["code"])
                d["name"] = str(r["name"])
                if ek and ek in r:
                    d["extra"] = r[ek]
                rows.append(d)
            out[key] = dict(key=key, title=title, desc=desc, cat=cat,
                            extra=ek, extra_title=et,
                            note=screens.NOTES.get(key), rows=rows)
        except Exception as e:                       # 單一榜單壞掉不該讓整批失敗
            out[key] = dict(key=key, title=title, desc=desc, cat=cat,
                            error=str(e)[:200], rows=[])
    return out


class Inconsistent(Exception):
    """產出的內容自相矛盾，不該發佈。"""


def selfcheck(p, funnel_data, screens_data):
    """寫檔前的後置檢查：產出的東西站不站得住腳。

    這個專案反覆出現同一種失敗 —— <b>某一層安靜地失效，頁面照樣顯示</b>：

      月營收沒帶進 CI      -> L3 刷掉 0 檔，名單 221 變 263
      pandas 解析度不一致  -> 同上，但原因完全不同
      run_screen 少了分支  -> 三個榜單變成 0 筆
      TWSE 資料源發布時差  -> 殖利率層刷掉 0 檔，名單虛胖近一倍

    每一次的共通點都是「沒有任何錯誤訊息」，而且都是靠人工對照數字才發現的。
    與其逐一堵住每個入口，不如在出口設一道檢查：<b>只要有任何一層的輸入
    整欄都是缺值，或任何榜單是空的，就拒絕產出</b>。

    寧可 CI 紅燈，也不要發佈一份看起來正常、實際上少一層篩選的名單。
    """
    bad = []
    day = p[p["date"] == p["date"].max()]

    # 1. 漏斗每一層的輸入欄位要真的有值
    need = {"amt20": "流動性", "div_yield": "殖利率", "pos252": "價格位置",
            "yoy": "月營收"}
    for col, label in need.items():
        if col not in day.columns:
            bad.append("{}：欄位 {} 根本不存在".format(label, col))
        elif not int(day[col].notna().sum()):
            bad.append("{}：最新一日整欄都是缺值，該層會刷掉 0 檔".format(label))

    # 2. 漏斗層數與刷除數
    steps = funnel_data["steps"]
    if len(steps) != len(funnel.LAYERS):
        bad.append("漏斗層數 {} 與定義的 {} 不符".format(len(steps), len(funnel.LAYERS)))
    for st in steps[1:]:
        # L1b（當天鎖漲跌停）本來就常常是 0，不算異常
        if st["key"] != "L1b" and not st["removed"]:
            bad.append("{}：刷掉 0 檔（這層可能失效了）".format(st["name"]))
    if not funnel_data["passed"]:
        bad.append("候選名單是空的")
    if funnel_data["passed"] == funnel_data["total"]:
        bad.append("候選名單等於全宇宙，等於沒有篩選")

    # 3. 每個榜單都要有內容
    for k, v in screens_data.items():
        if v.get("error"):
            bad.append("榜單 {} 產生失敗：{}".format(k, v["error"][:60]))
        elif not v["rows"] and k != "exdiv":     # 除權息可能真的沒有事件
            bad.append("榜單 {} 是空的".format(k))

    if bad:
        raise Inconsistent(
            "產出內容未通過自我檢查，已中止（沒有寫出任何檔案）：\n  - "
            + "\n  - ".join(bad))
    print("自我檢查通過：{} 層漏斗、{} 個榜單、候選 {} 檔".format(
        len(steps), len(screens_data), funnel_data["passed"]), flush=True)


def emit_stock(code, name, cat, pan):
    """單檔的完整內容。回傳 None 代表這檔不在可分析範圍。"""
    sub = factors.ETF_SUBCATS if cat == "etf" else None
    s, _ = stock_report.prepare(code, cat=cat, subcats=sub, panel=pan)
    if s is None or len(s) < 40:
        return None
    last = s.iloc[-1]
    prev = s.iloc[-2] if len(s) > 1 else last
    pc = float(prev["close"])
    chg = float(last["close"]) - pc
    pct = chg / pc * 100 if pc else 0.0

    chk = [dict(level=it[0], title=it[1], desc=it[2],
                key=(it[3] if len(it) > 3 else None))
           for it in server.build_checklist(s, code, cat)]
    sigs = [dict(name=n, value=v, n=k) for n, v, k in server.signal_rows(s)]

    tier = last.get("tier")
    tier = None if tier is None or pd.isna(tier) else int(tier)
    return dict(
        code=code, name=name, cat=cat, kind=server.KIND_LABEL.get(cat, cat),
        date=str(last["date"])[:10],
        close=float(last["close"]), chg=chg, chg_pct=pct,
        tier=tier, tier_text=(server.TIER_TXT.get(tier) if tier else None),
        n_days=int(len(s)),
        checklist=chk, signals=sigs, cost_pct=COST_ROUND_TRIP * 100,
        # 預先算好的 SVG。移植畫圖邏輯到 JS 最容易產生「看起來像但其實不同」。
        chart_svg=stock_report.candles_svg(s.tail(30)),
    )


# --------------------------------------------------------------------- 主流程
def build(limit=None, out=SITE):
    t0 = time.time()
    data = out / "data"

    print("載入面板…", flush=True)
    p_common = server.panel("common")
    p_etf = server.panel("etf")
    p_full = server.panel("common", full=True)
    day_date = p_common["date"].max()

    # 先全部算出來、檢查過，才動既有的 data/。
    # 順序很重要：原本是一開始就把 data/ 砍掉，中途失敗會留下一個空站。
    print("漏斗…", flush=True)
    fn = emit_funnel(p_full)
    print("榜單…", flush=True)
    sc = emit_screens(p_common)
    selfcheck(p_common, fn, sc)

    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)

    sizes = {}
    sizes["meta"] = write(data / "meta.json", emit_meta(p_common, day_date))
    sizes["universe"] = write(data / "universe.json", emit_universe())
    sizes["funnel"] = write(data / "funnel.json", fn)
    tot = 0
    for k, v in sc.items():
        tot += write(data / "screens" / "{}.json".format(k), v)
    sizes["screens"] = tot
    # 索引一份，前端不必先知道有哪些榜單
    write(data / "screens" / "_index.json",
          [dict(key=k, title=v["title"], cat=v["cat"], n=len(v["rows"]))
           for k, v in sc.items()])

    print("個股…", flush=True)
    u = server.universe()
    if limit:
        u = u.head(limit)
    ok = skip = 0
    stot = 0
    for i, (_, r) in enumerate(u.iterrows()):
        code, cat = str(r["code"]), r["cat"]
        pan = p_etf if cat == "etf" else p_common
        try:
            d = emit_stock(code, str(r["name"]), cat, pan)
        except Exception:
            d = None
        if d is None:
            skip += 1
            continue
        stot += write(data / "stocks" / "{}.json".format(code), d)
        ok += 1
        if (i + 1) % 200 == 0:
            print("  {}/{}  可用 {} 略過 {}".format(i + 1, len(u), ok, skip),
                  flush=True)
    sizes["stocks"] = stot

    total = sum(sizes.values())
    print("\n{:<12}{:>10}".format("區塊", "大小"))
    print("-" * 24)
    for k, v in sizes.items():
        print("{:<12}{:>9.1f} KB".format(k, v / 1024))
    print("-" * 24)
    print("{:<12}{:>9.1f} MB".format("合計", total / 1e6))
    print("\n個股 {} 檔可用、{} 檔略過（流動性不足／上市太短／已排除的 ETF）"
          .format(ok, skip))
    print("資料日期 {}   耗時 {:.0f} 秒".format(str(day_date)[:10], time.time() - t0))
    return sizes


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    build(limit=n)
