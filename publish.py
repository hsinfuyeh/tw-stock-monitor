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
import json
import multiprocessing as mp
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import factors
import funnel
import screens
import risk_lists
import server
import stable
import stock_report
import webui
from config import COST_ROUND_TRIP

ROOT = Path(__file__).parent
SITE = ROOT / "site"
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


def _dumps(obj):
    return json.dumps(_clean(obj), ensure_ascii=False, separators=(",", ":"))


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    txt = _dumps(obj)
    path.write_text(txt, encoding="utf-8")
    return len(txt.encode())


def _row_dict(r, cols):
    return {c: r.get(c) for c in cols if c in r}


# --------------------------------------------------------------------- 各區塊
LIST_COLS = ["code", "name", "close", "chg_pct", "amt20", "div_yield", "pos252",
             "yoy", "法人買超", "tier", "vol_ratio", "sd60"]


def _holidays():
    """TWSE 休市日（週間），給前端算「落後幾個交易日」。抓不到不擋發佈，前端退回只跳週末。"""
    try:
        import ingest
        return ingest.holidays()
    except Exception as e:
        print("::warning::休市日抓不到，網頁的落後天數只會跳過週末：{}".format(e), flush=True)
        return []


def emit_meta(p, day_date):
    """資料日期與各層證據強度。前端的過期橫幅也靠這個。"""
    return dict(
        data_date=str(day_date)[:10],
        holidays=_holidays(),
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


def emit_universe(include_otc=True):
    u = server.universe()
    if not include_otc:
        u = u[u["cat"] != "otc"]
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
    _add_ind(rows)
    return dict(date=str(dt_)[:10], steps=steps, total=steps[0]["after"],
                passed=steps[-1]["after"], rows=rows,
                order_note=funnel.ORDER_NOTE)


def emit_screens(p):
    out = {}
    for key, title, desc, cat in screens.SCREENS:
        if key == "screen":
            continue          # 已改為導向選股頁，不再單獨產出
        if key.startswith("social_"):
            continue          # 社群聲量另外產出（emit_social），不參加自我檢查
        try:
            if key == "exdiv":
                df = screens.upcoming_exdiv(p)
                rows = [dict(code=str(r["code"]), name=str(r["name"]),
                             date=str(r["date"])[:10],
                             days_left=int(r["days_left"]),
                             kind=str(r["kind"]),
                             value=r["cash"],            # NaN（待公告）會被 _clean 轉成 null
                             stock=float(r["stock"]),
                             rights=float(r["rights"]), rights_px=r["rights_px"],
                             pending=bool(r["pending"]),
                             yield_pct=r["yield_pct"])
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
    for v in out.values():
        _add_ind(v.get("rows"))
        _add_ind(v.get("rows_bottom"))
    return out


def _add_ind(rows):
    """每一列補上產業別（排行頁的表格統一欄位：代號、名稱、產業…）。"""
    import barrier
    if not rows:
        return
    ind = getattr(_add_ind, "_map", None)
    if ind is None:
        m = barrier.industry_map()
        ind = _add_ind._map = {c: barrier.IND_NAME.get(v) for c, v in m.items()}
    for r in rows:
        r["ind"] = ind.get(r["code"])


def _last_quotes():
    """每檔股票最新收盤與漲跌幅（上市 + 上櫃，不限流動性）。社群榜要列冷門股，panel 裡沒有。"""
    import store
    out = {}
    for t in ("quotes", "tpex_quotes"):
        try:
            q = store.q("""SELECT code, date, close FROM {t}
                           WHERE date >= (SELECT MAX(date) FROM {t}) - INTERVAL 10 DAY""".format(t=t))
        except Exception:
            continue
        q = q.dropna(subset=["close"]).sort_values(["code", "date"])
        for code, g in q.groupby("code"):
            c = g["close"].tolist()
            out[str(code)] = (c[-1], (c[-1] / c[-2] - 1) * 100 if len(c) > 1 and c[-2] else None,
                              t == "tpex_quotes")
    return out


def emit_social(day_date):
    """社群聲量兩張榜（social.py）。任何一步失敗都只回傳「抓不到」的空榜，不擋住發佈 ——
    這是附加資訊，選股主線不能因為 PTT 當機而停擺。"""
    info = {k: (t, d, c) for k, t, d, c in screens.SCREENS if k.startswith("social_")}

    def pack(key, rows, extra_title, status):
        t, d, c = info[key]
        return dict(key=key, title=t, desc=d, cat=c, extra="social", extra_title=extra_title,
                    note=screens.NOTES.get(key), rows=rows, social=status)

    try:
        import ingest
        import social
        cal = ["{}-{}-{}".format(x[:4], x[4:6], x[6:]) for x in ingest.trading_calendar()]
        dd = str(day_date)[:10]
        df, meta = social.ranking(dd, cal)
        meta["threads"] = "on" if social.threads_enabled() else "off"
    except Exception as e:
        print("::warning::社群聲量產出失敗，這次兩張榜顯示抓不到：{}".format(e), flush=True)
        st = dict(error=str(e)[:200])
        return {k: pack(k, [], "", st) for k in info}
    q = _last_quotes()
    names = social._names()

    def rows_of(d):
        rr = []
        for _, r in d.iterrows():
            code = str(r["code"])
            close, chg, otc = q.get(code, (None, None, False))
            rr.append(dict(code=code, name=names.get(code, code), close=close, chg_pct=chg, otc=otc,
                           extra=int(r["total"]), ptt_posts=int(r["ptt_posts"]),
                           ptt_pushes=int(r["ptt_pushes"]), threads=int(r["threads"]),
                           base=None if pd.isna(r["base"]) else round(float(r["base"]), 1),
                           ratio=None if pd.isna(r["ratio"]) else round(float(r["ratio"]), 2)))
        _add_ind(rr)
        return rr

    if not len(df):
        return {k: pack(k, [], "", meta) for k in info}
    hot = df.sort_values(["total", "ptt_posts"], ascending=False).head(screens.LIST_N)
    surge = df[(df["total"] >= social.MIN_MENTIONS) & df["base"].notna()]
    surge = surge.sort_values(["ratio", "total"], ascending=False).head(screens.LIST_N)
    enough = meta.get("base_windows", 0) >= 5       # 基準少於 5 個交易日，「暴增」沒有意義
    return {"social_hot": pack("social_hot", rows_of(hot), "聲量", meta),
            "social_surge": pack("social_surge", rows_of(surge) if enough else [],
                                 "比平常", dict(meta, too_new=not enough))}


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
        bad.append("候選名單等於全部股票，等於沒有篩選")

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


def active_index():
    """主動式 ETF 對每檔股票的持有與最近動作（activeetf.py）。只顯示、不計分。

    拿不到就回 None，個股頁那張卡片就不出現 —— 附加資訊不能擋住發佈。"""
    try:
        import activeetf
        import store
        hold = store.q("""
            SELECT h.etf, h.code, h.date, h.weight FROM {t} h
            JOIN (SELECT etf, MAX(date) d FROM {t} GROUP BY etf) m
              ON h.etf = m.etf AND h.date = m.d""".format(t=activeetf.HOLD_TABLE))
        mv = activeetf.moves(lookback=5)
        names = store.q("""SELECT code, name FROM quotes
                           WHERE date = (SELECT MAX(date) FROM quotes) AND code LIKE '00%A'""")
        try:
            names = pd.concat([names, store.q("""SELECT code, name FROM tpex_quotes
                           WHERE date = (SELECT MAX(date) FROM tpex_quotes) AND code LIKE '00%A'""")])
        except Exception:
            pass
    except Exception as e:
        print("::warning::主動 ETF 資料載入失敗，個股頁不顯示這一塊：{}".format(e), flush=True)
        return None
    nm = dict(zip(names["code"], names["name"]))
    out = {}
    for _, r in hold.iterrows():
        out.setdefault(r["code"], dict(holders=[], moves=[]))["holders"].append(dict(
            etf=r["etf"], name=nm.get(r["etf"], r["etf"]), date=str(r["date"])[:10],
            weight=None if pd.isna(r["weight"]) else float(r["weight"])))
    if len(mv):
        mv = mv[mv["kind"] != "持平"].sort_values("date", ascending=False)
        for _, r in mv.iterrows():
            out.setdefault(r["code"], dict(holders=[], moves=[]))["moves"].append(dict(
                etf=r["etf"], name=nm.get(r["etf"], r["etf"]), date=str(r["date"])[:10],
                kind=r["kind"], units_known=bool(r["units_known"]),
                chg=None if r["spu_chg"] is None or pd.isna(r["spu_chg"])
                else round(float(r["spu_chg"]) * 100, 1)))
    for v in out.values():
        v["holders"].sort(key=lambda x: -(x["weight"] or 0))
    print("主動 ETF：{} 檔 ETF、涵蓋 {} 檔股票".format(hold["etf"].nunique(), len(out)),
          flush=True)
    return out


PERF_WINDOWS = [("一週", 5), ("一個月", 20), ("一季", 60), ("半年", 120), ("一年", 250)]


def perf_vs_bench(s, bench):
    """近期表現 vs 0050：同一段期間的還原漲跌（%）。bench 是 0050 總報酬指數（以日期為索引）。

    期間用<b>市場的交易日曆</b>往回數（bench 的索引），不是數這檔股票自己的列數 ——
    個股序列會少掉停牌或被濾掉的日子，數列數會讓「一年」變成一年多（實測 0050 一年 +107% 被算成 +141%）。
    起點那天這檔沒有交易，就用那天之前最近的一筆。"""
    if bench is None or "adj" not in s:
        return None
    px = s[["date", "adj"]].dropna().set_index("date")["adj"].sort_index()
    if len(px) < 2:
        return None
    cal = bench.index[bench.index <= px.index[-1]]
    out = []
    for label, n in PERF_WINDOWS:
        if len(cal) <= n:
            continue
        d0, d1 = cal[-1 - n], cal[-1]
        a = px[:d0]
        if not len(a):                        # 上市不到這麼久
            continue
        s0, s1 = float(a.iloc[-1]), float(px.iloc[-1])
        b0, b1 = bench.get(d0), bench.get(d1)
        out.append(dict(label=label, stock=round((s1 / s0 - 1) * 100, 1),
                        bench=None if b0 is None or b1 is None or pd.isna(b0) or pd.isna(b1)
                        else round(float(b1 / b0 - 1) * 100, 1)))
    return out or None


def emit_stock(code, name, cat, pan, act=None, bench=None, stable_hist=None):
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
        active=(act or {}).get(code),
        perf=perf_vs_bench(s, bench),
        stable_hist=stable_hist,
    )


# --------------------------------------------------------------------- 個股頁（可以同時產生）
# 一千多檔個股頁彼此獨立，一檔一檔做在 CI 上要 70 秒左右。GitHub 的機器有 4 顆核心，
# 所以分給幾個子行程同時做。用 fork：子行程直接繼承下面這份共用資料（面板有 2 GB，
# 不可能序列化傳過去）。Windows 沒有 fork，本機照舊一檔一檔做，結果完全一樣。
_W = {}


def _stock_text(code, name, cat):
    """一檔個股頁的 JSON 文字；不在可分析範圍或產生失敗回 None。"""
    pan = _W["pans"].get(cat, _W["pans"]["common"])
    if pan is None:
        return None
    try:
        d = emit_stock(code, name, cat, pan, _W["act"], _W["bench"], _W["hist"].get(code))
    except Exception:
        return None
    return None if d is None else _dumps(d)


def _stock_chunk(jobs):
    """子行程做的事：一批股票，產出就直接寫檔，回報 [(代號, 位元組數或 None)]。"""
    out = []
    for code, name, cat in jobs:
        txt = _stock_text(code, name, cat)
        if txt is None:
            out.append((code, None))
            continue
        (_W["dir"] / "{}.json".format(code)).write_text(txt, encoding="utf-8")
        out.append((code, len(txt.encode())))
    return out


def _emit_stocks(jobs):
    """產出全部個股頁，回傳 (可用檔數, 略過檔數, 總位元組數)。"""
    n = int(os.environ.get("PUBLISH_WORKERS") or min(4, os.cpu_count() or 1))
    chunks = [jobs[i:i + 40] for i in range(0, len(jobs), 40)]
    res, parallel = [], False
    if n > 1 and len(chunks) > 1 and "fork" in mp.get_all_start_methods():
        # 先在這裡把每份面板的共用索引算好，子行程才不會各算一次
        for pan in _W["pans"].values():
            if pan is not None:
                stock_report._shared(pan)
        try:
            with mp.get_context("fork").Pool(n) as pool:
                for i, part in enumerate(pool.imap_unordered(_stock_chunk, chunks)):
                    res += part
                    if (i + 1) % 8 == 0:
                        print("  {}/{}".format(len(res), len(jobs)), flush=True)
            parallel = True
        except Exception as e:
            print("::warning::個股頁同時產生失敗，改成一檔一檔做：{}".format(e), flush=True)
            res = []
    if not parallel:
        for i, c in enumerate(chunks):
            res += _stock_chunk(c)
            if (i + 1) % 5 == 0:
                print("  {}/{}".format(len(res), len(jobs)), flush=True)
    done = {c: b for c, b in res if b is not None}
    if parallel:
        # 抽樣核對：同時做出來的，要跟這裡單獨重做一次的每個位元都一樣。
        # 子行程之間理論上互不影響，但這個專案的教訓是「安靜地不一樣」比出錯更糟，所以實際對一次。
        names = {c: (nm, cat) for c, nm, cat in jobs}
        sample = sorted(done)[::max(1, len(done) // 20)][:20]
        bad = [c for c in sample
               if _stock_text(c, *names[c]) != (_W["dir"] / "{}.json".format(c)).read_text(encoding="utf-8")]
        if bad:
            raise Inconsistent("個股頁同時產生的結果跟單獨產生的不一樣：{}".format("、".join(bad)))
        print("個股頁：{} 個子行程同時產生，抽樣 {} 檔逐位元核對一致".format(n, len(sample)), flush=True)
    return len(done), len(jobs) - len(done), sum(done.values())


# --------------------------------------------------------------------- 主流程
def build(limit=None, out=SITE):
    t0 = time.time()
    data = out / "data"

    print("載入面板…", flush=True)
    p_common = server.panel("common")
    p_etf = server.panel("etf")
    p_full = server.panel("common", full=True)
    day_date = p_common["date"].max()
    act = active_index()
    # 上櫃（只供個股頁查詢，見 tpex.py）。表還沒建或載入失敗都不擋住發佈 ——
    # 上櫃是附加資訊，上市那條主線不能因為它停擺。
    try:
        p_otc = server.panel("otc")
        print("上櫃面板：{:,} 列、{} 檔".format(len(p_otc), p_otc["code"].nunique()),
              flush=True)
    except Exception as e:
        print("::warning::上櫃面板載入失敗，這次不產上櫃個股頁：{}".format(e), flush=True)
        p_otc = None

    # 先全部算出來、檢查過，才動既有的 data/。
    # 順序很重要：原本是一開始就把 data/ 砍掉，中途失敗會留下一個空站。
    print("漏斗…", flush=True)
    fn = emit_funnel(p_full)
    print("榜單…", flush=True)
    sc = emit_screens(p_common)
    selfcheck(p_common, fn, sc)
    print("社群聲量…", flush=True)
    sc.update(emit_social(day_date))
    print("穩定強勢股名單、回測與名單成績（RESEARCH_LOG 第 11 輪）…", flush=True)
    feat = stable.features()
    ds = str(feat["date"].max())[:10]
    # 上線才有的排除：處置股、近 30 日注意股、全額交割。抓不到時照實標示，不當成「沒有」。
    risk = risk_lists.fetch(ds)
    att, err = risk_lists.recent_attention(ds)
    risk["attention30"] = att or []
    risk["attention30_status"] = "ok" if att is not None else "unavailable"
    if err:
        risk["attention30_error"] = err
    st = stable.compute(feat, risk)
    stable_hist = st[4] or {}
    del feat
    import barrier
    bench = barrier.market()["m_px"]           # 0050 總報酬指數，個股頁「近期表現 vs 大盤」用

    # 先寫到暫存目錄，全部寫完才換掉正式的 data/。
    # 原本是直接砍掉 data/ 再慢慢寫，本機按「立即更新」時，那 4 分鐘內
    # 開網頁會是空白或 404（CI 沒這個問題，它是先產出再部署）。
    final = data
    data = final.parent / "_data_new"
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)

    sizes = {}
    sizes["meta"] = write(data / "meta.json", emit_meta(p_common, day_date))
    sizes["universe"] = write(data / "universe.json", emit_universe(p_otc is not None))
    sizes["funnel"] = write(data / "funnel.json", fn)
    stable.write(data, *st)
    # 社群聲量的比對規則：網頁按「更新」時在瀏覽器裡讀 PTT、自己算聲量，用的是這份
    try:
        import social
        sizes["social_rules"] = write(data / "social_rules.json", social.rules())
    except Exception as e:
        print("::warning::社群比對規則寫不出來，網頁更新時不會帶社群聲量：{}".format(e), flush=True)
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
    jobs = [(str(r["code"]), str(r["name"]), r["cat"]) for _, r in u.iterrows()]
    _W.update(pans={"common": p_common, "etf": p_etf, "otc": p_otc}, act=act, bench=bench,
              hist=stable_hist, dir=data / "stocks")
    (data / "stocks").mkdir(parents=True, exist_ok=True)
    ok, skip, stot = _emit_stocks(jobs)
    sizes["stocks"] = stot

    # 原子換檔：舊的先改名、新的換上去、再刪掉舊的。中間只有毫秒級的空窗。
    old = final.parent / "_data_old"
    if old.exists():
        shutil.rmtree(old)
    if final.exists():
        final.rename(old)
    data.rename(final)
    shutil.rmtree(old, ignore_errors=True)
    data = final

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


def social_only(out=SITE):
    """快速通道：site/data 已經是「同一個資料日期、同一份程式碼」產出的，只重算社群聲量。

    網頁按「更新」時，多數情況行情與法人資料都沒變，會變的只有剛送進來的 PTT 聲量。
    整個重新產出要 4 分鐘；這裡只重寫兩張社群榜、比對規則與 meta.json 的產出時間，幾秒就好。
    其他檔案一個位元都不動 —— 所以只有在 workflow 確認快取的鍵（資料日期＋程式碼雜湊）
    完全對得上時才走這條路。任何一步不對就丟例外，由 workflow 退回完整產出。"""
    t0 = time.time()
    data = out / "data"
    meta = json.loads((data / "meta.json").read_text(encoding="utf-8"))
    idx_path = data / "screens" / "_index.json"
    index = json.loads(idx_path.read_text(encoding="utf-8"))
    day_date = meta["data_date"]
    sc = emit_social(day_date)
    for k, v in sc.items():
        if v.get("social", {}).get("error"):
            raise Inconsistent("社群聲量算不出來：{}".format(v["social"]["error"]))
        write(data / "screens" / "{}.json".format(k), v)
    by_key = {e["key"]: e for e in index}
    for k, v in sc.items():
        e = dict(key=k, title=v["title"], cat=v["cat"], n=len(v["rows"]))
        if k in by_key:
            by_key[k].update(e)
        else:
            index.append(e)
    write(idx_path, index)
    import social
    write(data / "social_rules.json", social.rules())
    # 產出時間要換：前端用它當版本參數，不換的話瀏覽器會繼續拿快取裡的舊社群榜
    meta["built_at"] = dt.datetime.now().isoformat(timespec="seconds")
    write(data / "meta.json", meta)
    print("快速通道：只更新社群聲量（資料日期 {}，{}）  耗時 {:.0f} 秒".format(
        day_date, "、".join("{} {} 筆".format(k, len(v["rows"])) for k, v in sc.items()),
        time.time() - t0), flush=True)


if __name__ == "__main__":
    if "--social-only" in sys.argv:
        social_only()
    else:
        n = int(sys.argv[1]) if len(sys.argv) > 1 else None
        build(limit=n)
