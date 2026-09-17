"""本機網站。

跑法： python server.py     然後開 http://127.0.0.1:5000

面板只在第一次請求時建一次（約 40 秒）並常駐記憶體，
之後切換股票是毫秒級 —— 這是「能隨時換股票查看」的前提。
"""
import datetime as dt
import html
import threading
import time

import numpy as np
import pandas as pd
from flask import Flask, jsonify, redirect, request

import factors
import rank as rankmod
import funnel
import likelihood
import screens
import stock_report
import store
import webui
from config import COST_ROUND_TRIP, DB
from run import CORE

app = Flask(__name__)
# 用 RLock 不是 Lock：重建倉儲期間必須擋住所有 DB 讀取（DuckDB 不允許
# 寫入連線與唯讀連線同時存在），而讀取路徑本身有巢狀
# （build_checklist 先查 exrights 再叫 panel），普通 Lock 會自己鎖死自己。
_lock = threading.RLock()
_cache = {}


def sq(sql, params=None):
    """所有 DB 查詢的唯一入口。更新倉儲時整個擋住，避免撞上 DuckDB 的檔案鎖。"""
    with _lock:
        return store.q(sql, params)


# --------------------------------------------------------------------- 資料
# 面板要用到的核心資料表。margin 不在其中（panel 沒有合併它）。
CORE_TABLES = ("quotes", "inst", "valuation")
# 對應的原始資料集（margin 不在內，panel 沒用它）
CORE_DATASETS = ("mi_index", "bwibbu", "t86")


def usable_last_date():
    """三張核心表都有資料的最後一天。

    TWSE 各資料源的發布時間不一樣：行情（MI_INDEX）約 14:30 就出來，
    估值（BWIBBU）與三大法人（T86）要更晚，融資券（MI_MARGN）更晚。
    所以每天下午都有一段時間「行情有了、估值還沒有」。

    如果直接拿 quotes 的最後一天當最新日，那一天的 div_yield / 法人買超
    會全是缺值，於是殖利率那層排除規則<b>靜默失效</b> —— 漏斗照跑、
    畫面照顯示、候選名單從 222 檔變成 407 檔，而且不會有任何錯誤。
    實測就是這樣發生的。

    取三張表的 MAX(date) 再取最小值，寧可少看一天，也不要用半套資料做決定。
    """
    try:
        cols = ", ".join(
            "(SELECT MAX(date) FROM {}) AS {}".format(t, t) for t in CORE_TABLES)
        r = store.q("SELECT " + cols)
        vals = [r[t][0] for t in CORE_TABLES]
        vals = [v for v in vals if v is not None and not pd.isna(v)]
        return min(vals) if vals else None
    except Exception:
        return None


def _check_fresh():
    """倉儲檔案變了就丟掉記憶體裡的面板。必須在 _lock 內呼叫。

    面板建一次要 40 秒，所以常駐記憶體是對的決定；但舊版是<b>永遠不重建</b>——
    跑完 run.py daily 更新完資料之後，網站還是顯示前一天的數字，
    而且畫面上沒有任何跡象，只能靠重啟 server。
    這跟日曆快取是同一類問題：安靜地給你過期的答案。
    """
    stamp = DB.stat().st_mtime_ns if DB.exists() else 0
    if _cache.get("_stamp") != stamp:
        _cache.clear()
        _cache["_stamp"] = stamp


def panel(kind="common", full=False):
    """full=True 回傳未過濾的面板（含不合格列），漏斗頁用。"""
    key = kind + ("_full" if full else "")
    with _lock:
        _check_fresh()
        if key not in _cache:
            sub = factors.ETF_SUBCATS if kind == "etf" else None
            d = factors.build(cat=kind, subcats=sub, horizons=(1, 3, 5, 10, 20),
                              filter_eligible=not full)
            # 併入三大法人買賣超。
            # 這是全系統唯一通過「十分位單調 + 樣本外仍為正」的訊號
            # （單調性 +0.88，樣本外毛價差 +0.23%/5日），但效果小於交易成本，
            # 所以只當參考資訊呈現，不做成買賣訊號。
            try:
                inst = store.q("SELECT date, code, foreign_net, trust_net, "
                               "total_net FROM inst")
                inst["date"] = pd.to_datetime(inst["date"])
                d = d.merge(inst, on=["date", "code"], how="left")
                d["法人買超"] = (d["total_net"] / d["vol20"]).replace(
                    [np.inf, -np.inf], np.nan)
                d["外資買超"] = (d["foreign_net"] / d["vol20"]).replace(
                    [np.inf, -np.inf], np.nan)
            except Exception:
                d["法人買超"] = np.nan
                d["外資買超"] = np.nan
            # 併入估值 + 計算一年價格位置。
            # 這兩項支撐系統中唯一通過 Bonferroni 校正的兩條「排除規則」：
            #   排除殖利率最低 40% -> 剩餘池子中位數 +5.49%/年 (t=4.69)
            #   排除價格位置最低 30% -> 剩餘池子平均 +2.93%/年 (t=2.96)
            # 是排除不是選擇 —— 排除的成本門檻是 0，選擇要先跨過 0.6% 手續費。
            try:
                v = store.q("SELECT date, code, pe, pb, div_yield FROM valuation")
                v["date"] = pd.to_datetime(v["date"])
                d = d.merge(v, on=["date", "code"], how="left")
            except Exception:
                d["pe"] = d["pb"] = d["div_yield"] = np.nan
            gg = d.sort_values(["code", "date"]).groupby("code", sort=False)
            hi = gg["high"].transform(
                lambda x: x.shift(1).rolling(252, min_periods=60).max())
            lo = gg["low"].transform(
                lambda x: x.shift(1).rolling(252, min_periods=60).min())
            d["pos252"] = (d["close"] - lo) / (hi - lo)
            # 當日橫斷面百分位，給排除規則用
            byday = d.groupby("date")
            d["ypct"] = byday["div_yield"].rank(pct=True)
            d["ppct"] = byday["pos252"].rank(pct=True)
            if "法人買超" in d:
                d["ipct"] = byday["法人買超"].rank(pct=True)
            # 歷史勝率等級：只用對勝率有實證貢獻的兩個成分等權合成。
            # 等級 1→5 的歷史勝率 46.2% → 53.6%（10 日，t=4.40，五級全單調）。
            # 月營收（point-in-time：依法規公布期限對齊，不會偷看未來）
            #
            # 這裡的例外一定要出聲。原本是靜靜地把 yoy 設成缺值，結果第一次
            # 部署到 CI 時 revenue.load() 失敗，漏斗的 L3 營收層整層失效，
            # 候選名單從 221 檔虛胖成 263 檔 —— 而唯一的線索是網頁上那個
            # 「刷掉 0 檔」，沒有任何錯誤訊息，花了兩輪才找到。
            try:
                import revenue
                rv = revenue.load()
                if len(rv):
                    pit = revenue.as_of_panel(rv, d["date"].unique())
                    d = d.merge(pit[["date", "code", "yoy", "yoy_3m"]],
                                on=["date", "code"], how="left")
                    n = int(d["yoy"].notna().sum())
                    print("  月營收：{:,} 筆原始、面板覆蓋 {:,} 列".format(len(rv), n),
                          flush=True)
                    if not n:
                        print("::warning::月營收讀到了但一列都對不上面板 —— "
                              "L3 營收層會整層失效。", flush=True)
                else:
                    print("::warning::revenue.load() 回傳空的，L3 營收層將失效。",
                          flush=True)
                    d["yoy"] = d["yoy_3m"] = np.nan
            except Exception as e:
                import traceback
                print("::error::月營收載入失敗，L3 營收層將失效：{}".format(e),
                      flush=True)
                traceback.print_exc()
                d["yoy"] = d["yoy_3m"] = np.nan
            d["L"] = likelihood.score(d)
            d["tier"] = d.groupby("date")["L"].transform(
                lambda s: pd.qcut(s.rank(method="first"), 5,
                                  labels=False, duplicates="drop") + 1)
            # 砍掉「只有行情、還沒有估值／法人」的那幾天（見 usable_last_date）
            cut = usable_last_date()
            if cut is not None:
                d = d[d["date"] <= pd.Timestamp(cut)]
            _cache[key] = d
        return _cache[key]


# --------------------------------------------------------------- 自動更新
# 原本的流程是「收盤後自己去終端機跑 run.py daily」。那是資料倉儲的思維，
# 對每天要用的工具來說不合理 —— 忘了跑就是安靜地看昨天的價格做今天的決定。
# 現在 server 自己顧：背景每 10 分鐘檢查一次，落後就補；也可以在畫面上手動按。
UPD = {"state": "idle", "msg": "", "detail": "", "at": None, "last_date": None}
_upd_lock = threading.Lock()

# 收盤 13:30，資料約 14:30 後上架；抓 15:00 保守一點
CLOSE_HOUR = 15


def behind_days(last):
    """資料落後幾個營業日。國定假日會多算，寧可多問一次也不要安靜地放過。"""
    now = dt.datetime.now()
    edge = now.date() if now.hour >= CLOSE_HOUR else now.date() - dt.timedelta(days=1)
    n, d = 0, last + dt.timedelta(days=1)
    while d <= edge:
        if d.weekday() < 5:
            n += 1
        d += dt.timedelta(days=1)
    return n


def data_last_date():
    """對外宣稱的「資料最新日」= 三張核心表都齊的那天，不是 quotes 的最後一天。"""
    try:
        with _lock:
            m = usable_last_date()
        return dt.date(m.year, m.month, m.day) if m is not None else None
    except Exception:
        return None


def _set(**kw):
    with _upd_lock:
        UPD.update(kw)


def do_update():
    """增量更新。只抓倉儲裡還沒有的交易日；沒有新的就什麼都不做。"""
    with _upd_lock:
        if UPD["state"] == "running":
            return False
        UPD.update(state="running", msg="檢查交易日曆…", detail="", at=dt.datetime.now())
    try:
        import ingest
        # 日曆的當月份一定重抓 —— 這裡曾經是整套流程停滯的原因
        cal = ingest.trading_calendar()

        # 用「三張核心表都齊的最後一天」當基準，不是用 quotes 的最後一天。
        # 用 quotes 的話會卡死：行情先發布、估值還沒有 -> quotes 拿到那天 ->
        # 之後 todo 永遠是空的 -> 估值再也補不進來，那一層排除規則就永久失效。
        last_ok = usable_last_date()
        cutoff = last_ok.strftime("%Y%m%d") if last_ok is not None else "00000000"
        todo = [d for d in cal[-20:] if d > cutoff]
        if not todo:
            _set(state="done", msg="已經是最新的", detail="", at=dt.datetime.now())
            return True

        _set(msg="下載 {} 個交易日…".format(len(todo)),
             detail="{} ~ {}".format(todo[0], todo[-1]))
        pairs = [(ds, d) for ds in ingest.DATASETS for d in todo]
        before = {x for x in pairs if ingest.have(*x)}
        ingest.backfill(list(ingest.DATASETS), todo)
        after = {x for x in pairs if ingest.have(*x)}

        # 要不要重建，看的是「倉儲有沒有落後原始檔」，不是「這次有沒有抓到新檔」。
        # 只看有沒有抓到新檔會漏掉一種情況：檔案上一輪就下載好了，但那一輪
        # 沒重建（或重建時檔案還沒到），於是資料永遠躺在硬碟上進不了資料庫。
        raw_ready = [d for d in todo
                     if all(ingest.have(ds, d) for ds in CORE_DATASETS)]
        if after == before and not raw_ready:
            # 沒有新檔案，原始檔也還沒湊齊 —— TWSE 那幾個資料源還沒發布。
            # 這時候絕對不能重建：重建要 3 分鐘且吃滿 CPU，而背景每 10 分鐘
            # 檢查一次，等於整個下午都在空轉重算同一份資料。
            _set(state="done", at=dt.datetime.now(),
                 msg="還沒有新資料",
                 detail="TWSE 尚未發布 {} 的完整資料（各資料源發布時間不同）".format(
                     todo[0]))
            return True

        _set(msg="重建倉儲…", detail="約需 3 分鐘，期間網站仍可瀏覽")
        with _lock:
            store.build(verbose=False)
            _cache.clear()          # 面板要跟著重算，否則畫面還是舊數字

        last = data_last_date()
        # 行情比估值／法人多出來的那幾天是不能用的，據實說明而不是宣稱成功
        try:
            qmax = sq("SELECT MAX(date) m FROM quotes").m[0]
            partial = (last is not None
                       and dt.date(qmax.year, qmax.month, qmax.day) > last)
        except Exception:
            partial = False
        _set(state="done", at=dt.datetime.now(), last_date=str(last),
             msg="已更新到 {:%Y-%m-%d}".format(last) if last else "已更新",
             detail=("行情已有更新的一天，但估值／法人資料尚未發布，"
                     "所以先不採用" if partial else ""))
        return True
    except Exception as e:
        _set(state="error", msg="更新失敗", detail=str(e)[:200], at=dt.datetime.now())
        return False


def update_async():
    if UPD["state"] == "running":
        return False
    threading.Thread(target=do_update, daemon=True).start()
    return True


def _auto_loop():
    """背景自動更新。刻意只在 __main__ 啟動 —— 被當模組 import 時不該偷跑網路。"""
    while True:
        try:
            last = data_last_date()
            if last and behind_days(last) >= 1 and UPD["state"] != "running":
                do_update()
        except Exception:
            pass
        time.sleep(600)


def universe():
    """搜尋用的清單（最新交易日仍在市場上的證券）。"""
    with _lock:
        _check_fresh()
        if "uni" not in _cache:
            _cache["uni"] = store.q("""
                SELECT code, name, cat, subcat FROM quotes
                WHERE date = (SELECT MAX(date) FROM quotes)
                  AND cat IN ('common','etf')
                ORDER BY code""")
        return _cache["uni"]


KIND_LABEL = {"common": "上市股", "etf": "ETF",
              "leveraged": "槓桿ETF", "inverse": "反向ETF",
              "bond": "債券ETF", "foreign": "海外ETF",
              "active": "主動式ETF", "dividend": "高股息ETF",
              "domestic": "台股ETF"}


def _kind_of(row):
    if row["cat"] == "etf":
        return KIND_LABEL.get(row["subcat"], "ETF")
    return "上市股"


# --------------------------------------------------------------------- 檢查項
def build_checklist(s, code, cat):
    """進場前該確認的事實。

    刻意不給「可以買 / 不要買」。這個工具已經被驗證無法預測，
    給結論等於把一個無效的東西包裝成建議。這裡只負責把該看的攤開。
    """
    last = s.iloc[-1]
    items = []

    # 1. 流動性 —— 決定你能放多大部位、進出是否會滑價
    amt = float(last["amt20"])
    if amt >= 5e8:
        lv, t = "ok", "流動性充足"
        d = "20 日均額 {:.1f} 億。一般散戶部位進出不會有問題。".format(amt / 1e8)
    elif amt >= 1e8:
        lv, t = "ok", "流動性正常"
        d = "20 日均額 {:.1f} 億。單筆控制在日均額的 1% 以內較安全。".format(amt / 1e8)
    elif amt >= 2e7:
        lv, t = "warn", "流動性偏低"
        d = ("20 日均額只有 {:.2f} 億。大單進出會推動價格，"
             "而且急著賣時可能賣不到理想價位。".format(amt / 1e8))
    else:
        lv, t = "bad", "流動性不足"
        d = ("20 日均額僅 {:.2f} 億。這個量級很難順利進出，"
             "報價也常是買賣價差跳動而非真實成交。".format(amt / 1e8))
    items.append((lv, t, d, "流動性"))

    # 2. 波動度 —— 決定部位大小與停損距離
    sd = float(last["sd60"]) * np.sqrt(252) * 100
    try:
        allsd = panel(cat)
        day = allsd[allsd["date"] == last["date"]]
        pct = float((day["sd60"] < last["sd60"]).mean() * 100)
    except Exception:
        pct = float("nan")
    daily = float(last["sd60"]) * 100
    # 用「相對全市場的百分位」而非絕對門檻。
    # 台股整體波動本來就高，用絕對值（例如 45%）會把中段股票誤標成高風險。
    if pct != pct:                       # 拿不到百分位時退回絕對門檻
        lv, t = ("ok", "波動偏低") if sd < 30 else (
            ("warn", "波動偏高") if sd < 55 else ("bad", "波動很高"))
        d = "年化波動 {:.0f}%，單日常見振幅約 ±{:.1f}%。".format(sd, daily)
    elif pct < 35:
        lv, t = "ok", "波動低於多數股票"
        d = ("年化波動 {:.0f}%，單日常見振幅約 ±{:.1f}%。"
             "只比市場上 {:.0f}% 的股票更波動，相對穩定。".format(sd, daily, pct))
    elif pct < 70:
        lv, t = "ok", "波動中等"
        d = ("年化波動 {:.0f}%，單日常見振幅約 ±{:.1f}%。"
             "比市場上 {:.0f}% 的股票更波動，屬於中段。".format(sd, daily, pct))
    elif pct < 90:
        lv, t = "warn", "波動偏高"
        d = ("年化波動 {:.0f}%，單日常見振幅約 ±{:.1f}%。"
             "比市場上 {:.0f}% 的股票更波動，部位要放小一點。".format(sd, daily, pct))
    else:
        lv, t = "bad", "波動很高"
        d = ("年化波動 {:.0f}%，單日常見振幅約 ±{:.1f}%。"
             "比市場上 {:.0f}% 的股票更波動 —— 同樣金額的部位，"
             "風險是中段股票的數倍。".format(sd, daily, pct))
    items.append((lv, t, d, "波動度"))

    # 3. 近期漲跌停 —— 鎖死時買不到也賣不掉
    nlim = last.get("limit_20d")
    nlim = 0 if nlim is None or pd.isna(nlim) else int(nlim)
    if nlim == 0:
        items.append(("ok", "近期無漲跌停",
                      "近 20 個交易日沒有觸及漲跌停，交易狀態正常。", "漲跌停"))
    else:
        items.append(("warn", "近期曾觸及漲跌停",
                      "近 20 個交易日觸及 {} 次。鎖死時買不到也賣不掉，"
                      "掛市價單風險高。".format(nlim), "漲跌停"))

    # 4. 除權息 —— 影響帳面損益的判讀
    try:
        ex = sq("""SELECT date, value, kind FROM exrights
                        WHERE code = ? ORDER BY date DESC LIMIT 40""", [code])
        ex["date"] = pd.to_datetime(ex["date"])
        today = pd.Timestamp(dt.date.today())
        fut = ex[ex["date"] >= today].sort_values("date")
        rec = ex[(ex["date"] < today) & (ex["date"] >= today - pd.Timedelta(days=45))]
        if len(fut):
            r = fut.iloc[0]
            items.append(("warn", "即將除權息",
                          "{:%Y-%m-%d} 除{}，金額 {:.2f} 元。"
                          "當天股價會相應調整，那不是下跌。".format(
                              r["date"], r["kind"], r["value"]), "除權息"))
        elif len(rec):
            r = rec.iloc[-1]
            items.append(("ok", "近期已除權息",
                          "{:%Y-%m-%d} 除{} {:.2f} 元。看 K 線時要記得那天的"
                          "價格落差是配息造成的。".format(r["date"], r["kind"], r["value"]),
                          "除權息"))
        else:
            items.append(("ok", "近期無除權息", "未來與最近 45 天內沒有除權息事件。",
                          "除權息"))
    except Exception:
        pass

    # 5. 價格位置 —— 這一項有實證：排除最低 30% 可讓剩餘池子平均 +2.93%/年
    ppct = last.get("ppct")
    w = s.tail(252)
    if len(w) >= 60:
        hi, lo, c = float(w["high"].max()), float(w["low"].min()), float(last["close"])
        pos = (c - lo) / (hi - lo) * 100 if hi > lo else 50.0
        rank_txt = ""
        lv = "ok"
        if ppct is not None and not pd.isna(ppct):
            rank_txt = "（全市場第 {:.0f} 百分位）".format(float(ppct) * 100)
            if float(ppct) < 0.30:
                lv = "bad"
        if lv == "bad":
            extra = ("<strong>落在全市場最低 30%，建議排除。</strong>"
                     "實測排除這一群後，剩餘池子的平均超額報酬提升 2.93%／年"
                     "（t = 2.96，四段樣本全部同向）。")
        elif pos >= 90:
            extra = "接近一年來的高點。這不代表該賣，但回檔空間比較大。"
        else:
            extra = "位於區間中上段，不在建議排除的範圍。"
        items.append((lv, "價格位置",
                      "近一年區間的 {:.0f}%{}。{}".format(pos, rank_txt, extra),
                      "價格位置"))

    # 6. 殖利率位階 —— 排除最低 40% 可讓剩餘池子的「中位數」+5.49%/年
    ypct, dy = last.get("ypct"), last.get("div_yield")
    if ypct is not None and not pd.isna(ypct):
        if float(ypct) < 0.40:
            items.append(("bad", "殖利率位階偏低",
                          "殖利率 {:.2f}%，全市場第 {:.0f} 百分位。"
                          "<strong>落在最低 40%，建議排除。</strong>"
                          "實測排除這一群後，剩餘池子的<strong>中位數</strong>報酬提升 "
                          "5.49%／年（t = 4.69）—— 注意是中位數不是平均數，"
                          "低殖利率股贏的次數少但偶爾大贏。".format(
                              float(dy) if dy == dy else 0, float(ypct) * 100),
                          "殖利率位階"))
        else:
            items.append(("ok", "殖利率位階正常",
                          "殖利率 {:.2f}%，全市場第 {:.0f} 百分位，"
                          "不在建議排除的範圍。".format(
                              float(dy) if dy == dy else 0, float(ypct) * 100),
                          "殖利率位階"))
    return items


TIER_TXT = {
    5: ("ok", "歷史上條件相似的案例，<b>53.6%</b> 在後續 10 日跑贏市場中位數"),
    4: ("ok", "歷史上條件相似的案例，<b>51.3%</b> 在後續 10 日跑贏市場中位數"),
    3: ("", "歷史上條件相似的案例，<b>50.4%</b> 在後續 10 日跑贏市場中位數"),
    2: ("warn", "歷史上條件相似的案例，<b>48.3%</b> 在後續 10 日跑贏市場中位數"),
    1: ("warn", "歷史上條件相似的案例，<b>46.2%</b> 在後續 10 日跑贏市場中位數"),
}


def _tier_block(last):
    """歷史勝率等級。

    刻意用「歷史上條件相似的案例有多少比例跑贏」這個說法，而不是「上漲機率」——
    前者是可查證的歷史頻率，後者是對未來的機率宣稱，本系統沒有能力做後者。
    """
    t = last.get("tier")
    if t is None or pd.isna(t):
        return ""
    t = int(t)
    lv, txt = TIER_TXT.get(t, ("", ""))
    stars = "●" * t + "○" * (5 - t)
    return webui.statusbar(
        lv, "歷史勝率等級 <b>{} / 5</b>　{}　—　{}。".format(
            t, stars, txt),
        "<b>這是查表，不是預測。</b>做法是把全市場依「殖利率位階 + 價格位置」"
        "等權排序切成五級，然後統計歷史上每一級有多少比例在後續 10 個交易日"
        "跑贏當日市場中位數。<br><br>"
        "五級的歷史勝率：46.2% → 48.3% → 50.4% → 51.3% → 53.6%"
        "（梯度 +6.84pp，t = 4.40，五級全單調，前後半樣本各自都成立）。<br><br>"
        "<b>請注意幅度</b>：最高級也只有 53.6%，跟丟銅板的 50% 只差 3.6 個百分點，"
        "而且信賴區間是 45.5%~61.6%。它能做的是<b>幫你排優先順序</b>，"
        "不是告訴你哪一檔會漲。<br><br>"
        "<b>不含</b>：K 線型態（資訊量近零）、綜合評分（反指標）、"
        "法人買超（對勝率無貢獻）。")


def signal_rows(s):
    """六種型態的真實資訊量（命中日減未命中日，10 日）。"""
    st = stock_report.rule_stats(s)
    out = []
    for _, r in st.iterrows():
        v = r.get("net10")
        if v is None or pd.isna(v):
            continue
        out.append((r["規則"], float(v), int(r["案例數"])))
    return out


# --------------------------------------------------------------------- 路由
@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    u = universe()
    m = u[u["code"].str.contains(q, case=False, na=False)
          | u["name"].str.contains(q, case=False, na=False)]
    m = m.head(12)
    return jsonify([dict(code=r["code"], name=r["name"], kind=_kind_of(r))
                    for _, r in m.iterrows()])


@app.route("/")
def home():
    try:
        cov = sq("""SELECT COUNT(DISTINCT date) d, MAX(date) b,
                         COUNT(DISTINCT code) c FROM quotes""")
        days, last, n = int(cov.d[0]), cov.b[0], int(cov.c[0])
        meta = "資料涵蓋 {:,} 個交易日，最新 {:%Y-%m-%d}，共 {:,} 檔證券".format(
            days, last, n)
    except Exception:
        meta = "倉儲尚未建立"
    pop = ["2330", "2317", "2454", "2308", "0050", "0056", "2412", "1101"]
    chips = "".join('<a class="chip" href="/s/{0}">{0}</a>'.format(c) for c in pop)
    body = """<div class="wrap">
<div class="homehero">
  <span class="eyebrow">個股查詢</span>
  <h1>查一檔股票</h1>
  {search}
  <div class="hint">輸入代號或名稱，例如 <strong>2330</strong> 或 <strong>台積電</strong>。
  任何時候按 <kbd>/</kbd> 都會跳回這個框。</div>
  <div class="chips">{chips}</div>
</div>
<div class="card">
  <h2>這個工具能做什麼</h2>
  <p class="lead">{meta}</p>
  <ul class="note">
  <li><strong>能做：</strong>把進場前該確認的事實攤開 —— 流動性夠不夠你的部位、
  波動你能不能承受、近期有沒有漲跌停、有沒有除權息、價格在什麼位置。</li>
  <li><strong>能做：</strong>誠實回答「常見的技術型態在這檔股票上有沒有用」。</li>
  <li><strong>不能做：</strong>預測漲跌、告訴你該買什麼。這不是保守的說法 ——
  是這套系統用 1,869 個交易日的真實資料驗證後的結果。</li>
  </ul>
</div>
<div class="card">
  <h2>已經驗證過的事</h2>
  <ul class="note">
  <li>六種常見 K 線型態（長紅、長黑、十字、爆量、突破、跌破）的預測力接近零，
  訊號強度小於 0.6% 的來回交易成本。</li>
  <li>10 日換倉的策略光成本就吃掉年化約 25%。</li>
  <li>「最近有效的指標」不能拿來用 —— 指標有效性的符號持續率只有 44~56%，
  等同丟銅板。</li>
  </ul>
</div>
</div>""".format(chips=chips, meta=html.escape(meta),
                 search=webui.searchbox(big=True))
    return page("台股觀測", body)


@app.route("/s/<code>")
def stock(code):
    code = code.strip().upper()
    u = universe()
    row = u[u["code"] == code]
    if not len(row):
        return page("找不到 " + code, """<div class="wrap"><div class="empty">
        <h2>找不到 {}</h2><div class="note">請確認代號，或在上面重新搜尋。<br>
        目前只涵蓋上市普通股與 ETF，尚未包含上櫃。</div></div></div>""".format(
            html.escape(code)), q=code), 404
    row = row.iloc[0]
    cat = row["cat"]
    sub = factors.ETF_SUBCATS if cat == "etf" else None
    s, _ = stock_report.prepare(code, cat=cat, subcats=sub, panel=panel(cat))
    if s is None or len(s) < 40:
        return page(code, """<div class="wrap"><div class="empty">
        <h2>{} {}</h2><div class="note">這檔目前不在可分析範圍內。<br>
        常見原因：成交量太低未達流動性門檻、上市時間太短、或屬於已排除的
        槓桿／反向／債券／海外型 ETF。</div></div></div>""".format(
            html.escape(code), html.escape(str(row["name"]))), q=code)

    last = s.iloc[-1]
    prev = s.iloc[-2] if len(s) > 1 else last
    chg = float(last["close"]) - float(prev["close"])
    pct = chg / float(prev["close"]) * 100 if float(prev["close"]) else 0.0
    cls = "up" if chg > 0 else ("dn" if chg < 0 else "mut")

    chk = webui.checklist(build_checklist(s, code, cat))
    tier_html = _tier_block(last)
    sigs = signal_rows(s)
    bars = webui.signal_bars([(n, v) for n, v, _ in sigs], cost_pct=COST_ROUND_TRIP * 100)
    cost_pct = COST_ROUND_TRIP * 100
    if sigs:
        nm, val, nhit = max(sigs, key=lambda x: abs(x[1]))
    else:
        nm, val, nhit = "", 0.0, 0
    if abs(val) < cost_pct:
        verdict = ("<strong>這些型態在這檔股票上沒有可用的預測力。</strong>"
                   "最強的訊號也只有 {:.2f}%，小於來回交易成本 {:.1f}%"
                   "（圖上的黃線）—— 就算方向猜對，也賺不回手續費和證交稅。"
                   .format(abs(val), cost_pct))
    elif val < 0:
        # 負訊號要講清楚方向，否則「超過成本」會被讀成「可以用」
        verdict = ("唯一超過交易成本的是<strong>「{}」，而且是負的（{:.2f}%）</strong>——"
                   "意思是這個型態出現後，這檔股票的表現反而比平常差。"
                   "要靠它獲利必須放空，但台股放空受限（平盤下不得放空、借券成本、"
                   "回補風險），實務上很難執行。而且只有 {} 次樣本，不足以當作依據。"
                   .format(nm, val, nhit))
    else:
        verdict = ("最強的是<strong>「{}」{:+.2f}%</strong>，略高於交易成本 {:.1f}%。"
                   "但這只有 {} 次樣本，而且是單一股票上的結果 —— "
                   "樣本數這麼少時，這種幅度很可能只是運氣。"
                   .format(nm, val, cost_pct, nhit))

    w30 = s.tail(30)
    chart = stock_report.candles_svg(w30)

    hits = stock_report._hits_table(s, last_n=25)
    stats = stock_report._stats_table(stock_report.rule_stats(s))

    body = """<div class="wrap">
<div class="hero"><h1>{name} <span class="code">{code}</span></h1>
  <div class="px"><div class="v">{close:,.2f}</div>
  <div class="d {cls}">{chg:+,.2f} ({pct:+.2f}%)</div></div></div>
<div class="meta">{kind} ｜ 資料截止 {asof:%Y-%m-%d} ｜ 統計樣本 {n:,} 個交易日</div>
{hint}

{status}

{tier}
<div class="card">
  <h2>進場前先確認這幾件事</h2>
  <p class="lead">這些是會實際影響你能否順利買賣、以及該放多大部位的事實。</p>
  {chk}
</div>

<div class="card">
  <h2>最近 30 個交易日</h2>
  <p class="lead">{tipK}：紅漲綠跌。{tipDot}代表當天出現了至少一種常見型態，滑過可看是哪幾種。</p>
  {chart}
</div>

<div class="card">
  <h2>那些技術型態，能告訴你什麼？</h2>
  <p class="lead">每種型態出現後 10 個交易日的表現，已經扣掉大盤漲跌，也扣掉這檔股票本身的漲跌趨勢。
  剩下的才是型態本身提供的{tipInfo}。圖上兩條黃線是{tipCost}的位置，
  長條沒有超過黃線就代表沒有可操作性。</p>
  {bars}
  <p class="note" style="margin-top:14px">{verdict}</p>
  <details><summary>看詳細數據</summary><div class="inner">
    <h3 style="font-size:14px;margin:0 0 8px">各型態的完整統計</h3>
    {stats}
    <h3 style="font-size:14px;margin:20px 0 8px">最近的型態出現日與後續表現</h3>
    {hits}
  </div></details>
</div>

<details><summary>這個頁面沒有告訴你的事</summary><div class="inner">
  <ul class="note">
  <li>公司基本面、產業前景、財報、法說會內容 —— 這些都不在這套資料裡。</li>
  <li>是否為處置股或注意股（會改成分盤交易，影響進出）—— 資料源還沒接。</li>
  <li>未來會怎麼走。沒有任何工具知道，這個也不例外。</li>
  </ul></div></details>
</div>""".format(
        name=html.escape(str(row["name"])), code=html.escape(code),
        close=float(last["close"]), cls=cls, chg=chg, pct=pct,
        kind=_kind_of(row), asof=last["date"], n=len(s),
        chk=chk, chart=chart, bars=bars, verdict=verdict,
        stats=stats, hits=hits,
        tier=tier_html,
        status=webui.statusbar(
            "", "這裡只呈現<b>已經發生的事實</b>，不預測未來、不給買賣建議。",
            "本系統用 2019–2026 共 1,869 個交易日驗證過：六種常見 K 線型態的資訊量"
            "接近零（+0.03% ~ −1.29%），全部小於 0.6% 的來回交易成本；"
            "因子合成的排序分數則是<b>反指標</b>（單調性 −0.61，t = −4.44）。<br><br>"
            "唯一通過「十分位單調 + 樣本外為正」的是<b>三大法人買超</b>"
            "（單調性 +0.88，樣本外毛價差 +0.23%/5 日）—— 這是真訊號，"
            "但效果小於 5 日換倉約 0.97% 的成本，所以只當參考資訊，不做成買賣訊號。"),
        hint=webui.TIP_HINT,
        tipK=webui.tip("K線"), tipDot=webui.tip("黃點"),
        tipInfo=webui.tip("資訊量"), tipCost=webui.tip("交易成本"))
    return page("{} {}".format(code, row["name"]), body, q=code)


# --------------------------------------------------------------------- 盤後
# 依「這份清單在回答什麼問題」分組，不是依產生它的程式碼分組。
#   價量    今天錢流去哪、誰動得最兇
#   位置    價格站在哪個相對位置
#   籌碼    誰在買賣
#   事件    接下來要發生什麼
#   排除法  套用驗證過的規則後還站著的（性質完全不同，所以獨立一組）
#   反指標  已證實方向相反的，留著當警示（同上）
# 分組依「這份清單的證據等級」，不是依主題。
# 實測過每個榜單的前 40 檔持有 10 日相對市場的超額（148 個非重疊日期）：
#   價格位置 +0.78 (t=3.79)   月營收 +0.47 (t=3.19)   <- 通過 Bonferroni（15 次檢定）
#   成交額   +0.62 (t=2.55)   跌幅   +0.35 (t=2.07)   法人買 +0.30 (t=2.23)
#   漲幅 +0.26  創新高 +0.20  爆量 +0.14  法人賣 -0.14  低波動 -0.34  <- 都不顯著
#   創新低   -0.39 (t=-2.89)  綜合評分 -0.07           <- 反指標那一組
TAB_GROUPS_SPEC = [
    ("已驗證", ["pos", "rev"]),
    ("今日事實", ["amount", "gain", "loss", "volume", "high"]),
    ("風險", ["calm"]),
    ("籌碼", ["inst", "instout"]),
    ("事件", ["exdiv"]),
    ("反指標", ["low", "score"]),
]
TAB_GROUPS = [
    (g, [(k, screens.TAB_LABEL.get(k, screens.SCREEN_MAP[k][0]),
          screens.SCREEN_MAP[k][2] == "score") for k in keys])
    for g, keys in TAB_GROUPS_SPEC
]


def _row(r, extra_key, fmt, show_inst=True, show_tier=False):
    code = str(r["code"])
    chg = r.get("chg_pct")
    chg_html = "—"
    if chg is not None and not pd.isna(chg):
        cls = "up" if chg > 0 else ("dn" if chg < 0 else "mut")
        chg_html = '<span class="{}">{:+.2f}%</span>'.format(cls, chg)
    v = r.get(extra_key) if extra_key else None
    inst_html = ""
    if show_inst:
        inst = r.get("法人買超")
        if inst is None or pd.isna(inst):
            cell = '<span class="mut">—</span>'
        else:
            icls = "up" if inst > 0 else ("dn" if inst < 0 else "mut")
            cell = '<span class="{}">{:+.1f}%</span>'.format(icls, float(inst) * 100)
        inst_html = "<td>{}</td>".format(cell)
    tier_html = ""
    if show_tier:
        tv = r.get("tier")
        if tv is None or pd.isna(tv):
            tier_html = '<td class="mut">—</td>'
        else:
            tv = int(tv)
            col = "var(--ok)" if tv >= 4 else ("var(--mut)" if tv == 3 else "var(--warn)")
            tier_html = ('<td class="sorted" style="color:{}">{} / 5</td>'
                         .format(col, tv))
    return ("<tr><td><a href=\"/s/{c}\"><strong>{c}</strong></a></td>"
            "<td><a href=\"/s/{c}\" style=\"color:var(--fg)\">{n}</a></td>"
            "{tier}<td>{p:,.2f}</td><td>{g}</td><td{x}>{e}</td>{i}"
            "<td class=\"mut\">{a}</td></tr>").format(
        tier=tier_html, x=("" if show_tier else ' class="sorted"'),
        c=html.escape(code), n=html.escape(str(r["name"])[:10]),
        p=float(r["close"]), g=chg_html, i=inst_html,
        e=(fmt(v) if v is not None and not pd.isna(v) else "—"),
        a="{:.2f}億".format(float(r["amt20"]) / 1e8))


def _table(rows, extra_title, extra_key, fmt, show_tier=False):
    # 主欄位本身就是法人買超時，不要再重複一欄
    show_inst = extra_key != "法人買超"
    body = "".join(_row(r, extra_key, fmt, show_inst, show_tier)
                   for _, r in rows.iterrows())
    if not body:
        return '<div class="empty"><div class="note">今天沒有符合條件的標的。</div></div>'
    inst_th = "<th>{}</th>".format(webui.tip("法人買超")) if show_inst else ""
    # 有等級欄時，排序依據是等級，所以 sorted 標記移到等級那一欄
    tier_th = ('<th class="sorted">{}</th>'.format(webui.tip("歷史勝率等級"))
               if show_tier else "")
    extra_cls = "" if show_tier else ' class="sorted"'
    return ('<div class="scroll"><table><thead><tr><th>代號</th><th>名稱</th>'
            + tier_th +
            "<th>{c}</th><th>{g}</th><th{x}>{e}</th>{i}<th>{a}</th>"
            "</tr></thead><tbody>{b}</tbody></table></div>".format(
                c=webui.tip("收盤"), g=webui.tip("今日"), x=extra_cls,
                e=webui.tip(extra_title), i=inst_th,
                a=webui.tip("20日均額"), b=body))


@app.route("/lists")
@app.route("/lists/<key>")
def lists(key="amount"):
    # 體質篩選跟 /funnel 做的是同一件事，而 funnel 是完整版（多了鎖漲跌停與營收層）。
    # 留兩個近乎一樣的入口只會讓人以為它們是不同的東西，所以這裡直接導過去。
    if key == "screen":
        return redirect("/funnel")
    if key not in screens.SCREEN_MAP:
        key = "amount"
    title, desc, cat = screens.SCREEN_MAP[key]
    p = panel("common")
    tabbar = webui.tabs(TAB_GROUPS, key)

    if key == "exdiv":
        df = screens.upcoming_exdiv(p)
        if not len(df):
            inner = '<div class="empty"><div class="note">未來 60 天內沒有除權息事件。</div></div>'
        else:
            rows = "".join(
                "<tr><td><a href=\"/s/{c}\"><strong>{c}</strong></a></td>"
                "<td>{n}</td><td>{d:%m-%d}</td><td>{L} 天後</td>"
                "<td>{v:.2f} 元</td><td>{y:.2f}%</td></tr>".format(
                    c=html.escape(str(r["code"])), n=html.escape(str(r["name"])[:10]),
                    d=r["date"], L=int(r["days_left"]),
                    v=float(r["value"]), y=float(r["yield_pct"]))
                for _, r in df.iterrows())
            inner = ('<div class="scroll"><table><thead><tr><th>代號</th><th>名稱</th>'
                     "<th>{d}</th><th>還有</th><th>{v}</th><th>{y}</th>"
                     "</tr></thead><tbody>{b}</tbody></table></div>".format(
                         d=webui.tip("除權息日", "除權息"), v=webui.tip("配發"),
                         y=webui.tip("換算幅度"), b=rows))
        note = ("除權息當天股價會往下調整那個幅度，那不是下跌，是配息被發出去了。"
                "想參加配息要在除權息日<strong>前一個交易日</strong>收盤前持有。"
                "配息要併入所得，高幅度的不一定划算。")

    elif key == "screen":
        day, dt_ = screens._latest(p)
        prev = screens._prev_close(p, dt_)
        day["prev"] = day["code"].map(prev)
        day["chg_pct"] = (day["close"] / day["prev"] - 1) * 100
        n0 = len(day[day["amt20"] >= 2e7])
        keep = ((day["amt20"] >= 2e7)
                & ~(day["ypct"] < 0.40).fillna(False)
                & ~(day["ppct"] < 0.30).fillna(False))
        # 兩階段：先排除，再對倖存者依歷史勝率等級排序。
        # 同一等級內改依流動性排序 —— 等級只有 5 級很粗，同級內用分數排序
        # 會把日成交額 0.2 億的標的頂到最前面，人工複核時等於浪費時間。
        rows = day[keep].sort_values(["tier", "amt20"], ascending=False).head(50)
        f = lambda v: "{:.2f}%".format(float(v)) if v == v else "—"
        inner = _table(rows, "殖利率", "div_yield", f, show_tier=True)
        note = ("原始可交易池 {:,} 檔 → 排除後剩 {:,} 檔（淘汰 {:.0f}%），"
                "再依歷史勝率等級由高到低排序。<br>"
                "<strong>等級不是漲跌預測</strong>，是「歷史上條件相似的案例有多少比例"
                "跑贏市場中位數」。最高級 53.6%、最低級 46.2%——差距只有 7 個百分點，"
                "用途是<strong>排優先順序讓你決定先看哪幾檔</strong>，"
                "後面的功課一樣都不能少。"
                .format(n0, int(keep.sum()), (1 - keep.sum() / max(n0, 1)) * 100))

    elif key == "score":
        top, bot = screens.score_rows(p)
        f = lambda v: "{:+.2f}".format(float(v))
        inner = ("<h3 style=\"font-size:14px;margin:18px 0 8px\">分數最高 25 名</h3>"
                 + _table(top, "綜合分數", "_s", f)
                 + "<h3 style=\"font-size:14px;margin:26px 0 8px\">分數最低 25 名</h3>"
                 + _table(bot, "綜合分數", "_s", f))
        note = None

    else:
        rows, ek, et = screens.run_screen(p, key)
        if ek in ("chg_pct", "over", "annvol"):
            f = lambda v: "{:+.2f}%".format(float(v)) if ek != "annvol" else "{:.0f}%".format(float(v))
        elif ek == "vol_ratio":
            f = lambda v: "{:.2f} 倍".format(float(v))
        elif ek == "amount":
            f = lambda v: "{:.1f}億".format(float(v) / 1e8)
        elif ek == "posv":
            # 可能超過 100% —— 分母的高點是 shift(1) 的近一年高，
            # 今天的收盤高過它就代表創了一年新高，不是算錯。
            f = lambda v: "{:.0f}%".format(float(v))
        elif ek == "yoy":
            f = lambda v: "{:+.1f}%".format(float(v))
        elif ek == "法人買超":
            f = lambda v: "{:+.1f}%".format(float(v) * 100)
        else:
            f = lambda v: str(v)
        inner = _table(rows, et, ek, f)
        note = None

    # 警語文字統一放在 screens.NOTES，本機版與靜態站共用同一份。
    # 寫死在這裡的話，靜態站只能各抄一份，兩邊遲早漂移。
    warn = ""
    if key in screens.NOTES:
        warn = webui.statusbar(*screens.NOTES[key])

    # 標題跟著內容走：除權息是「接下來要發生的事」，不是「今天收盤的結果」。
    # 導覽列寫除權息、頁面標題卻寫今日盤後，讀起來像點錯頁。
    if key == "exdiv":
        h1 = "除權息行事曆"
        metaline = "未來 60 天內的除權息事件 ｜ 資料截止 {:%Y-%m-%d} ｜ 點代號可看個股詳情".format(
            p["date"].max())
    else:
        h1 = "今日盤後"
        metaline = ("資料截止 {:%Y-%m-%d} ｜ 已套用流動性門檻"
                    "（20 日均額 2,000 萬以上）｜ 點代號可看個股詳情".format(p["date"].max()))

    body = """<div class="wrap">
<h1 style="font-size:30px;letter-spacing:-.03em;font-weight:500;margin:0 0 4px">{h1}</h1>
<div class="meta">{metaline}</div>
{hint}
{tabbar}
{warn}
<div class="card">
  <h2>{title}</h2>
  <p class="lead">{desc}</p>
  {inner}
  {note}
</div>
<details><summary>怎麼用這些榜單</summary><div class="inner">
  <ul class="note">
  <li><strong>這些是「發現工具」，不是「買進清單」。</strong>
  用來找出值得進一步研究的標的，接下來該做的是看公司在做什麼、財報如何、
  為什麼今天會這樣動 —— 那些都不在這套資料裡。</li>
  <li><strong>漲幅榜不是明日續漲榜。</strong> 已經漲完的事實，跟明天會怎樣是兩回事。
  本系統實測「突破前 20 日高」這類型態的資訊量只有 +0.33%，小於 0.6% 的交易成本。</li>
  <li><strong>跌幅榜要特別小心。</strong> 下跌通常有原因，而那個原因往往還沒反映完。</li>
  <li>所有榜單都排除了 20 日均額低於 2,000 萬的標的 ——
  那些股票的報酬是買賣價差跳動，而且你實際上買不太到。</li>
  </ul></div></details>
</div>""".format(h1=h1, metaline=metaline,
                 hint=webui.TIP_HINT, tabbar=tabbar, warn=warn,
                 title=html.escape(title), desc=html.escape(desc), inner=inner,
                 note=('<p class="note" style="margin-top:12px">{}</p>'.format(note)
                       if note else ""))
    return page(h1 if key == "exdiv" else "盤後 · " + title, body,
                      nav=("exdiv" if key == "exdiv" else "lists"))


# --------------------------------------------------------------------- 漏斗
SHORT_RULE = {"L0": "上市普通股", "L1": "均額 < 2,000 萬", "L1b": "開高低收同價",
              "L2a": "全市場後 40%", "L2b": "一年區間後 30%", "L3": "年增率 < 0"}


def _chips(steps):
    """條件 chips：每個條件標示自己刷掉幾檔。

    這是篩選器介面的通用慣例（Airbnb、FinViz 都這樣做）——
    使用者一眼看到「哪個條件最嚴格」，不必去讀圖。
    """
    out = []
    for st in steps[1:]:
        ev = st["evidence"]
        zero = "" if st["removed"] else " zero"
        off = "" if st["removed"] else " off"
        tip = html.escape("{}｜{}<br><br>{}".format(
            st["name"], SHORT_RULE.get(st["key"], st["rule"]), st["detail"]),
            quote=True)
        out.append(
            '<span class="fchip tip{off}" data-tip="{tip}" tabindex="0">'
            '<span class="dot {ev}"></span>'
            '<span class="lbl">{nm}</span>'
            '<span class="cnt{z}">−{n:,}</span></span>'.format(
                off=off, tip=tip, ev=ev, nm=html.escape(st["name"]),
                z=zero, n=st["removed"]))
    return '<div class="fchips">{}</div>'.format("".join(out))


def _waterfall(steps, total):
    """橫向遞減長條：藍色是留下的，紅色尾巴是這一層刷掉的。

    左對齊且同一基準尺規，所以長度差直接對應檔數差 —— 比漏斗形狀好讀。
    """
    rows = []
    for i, st in enumerate(steps):
        keep = st["after"] / max(total, 1) * 100
        drop = st["removed"] / max(total, 1) * 100
        last = i == len(steps) - 1
        pending = st["key"] == "L3" and not st["removed"]
        badge = ""
        if i:
            ev = st["evidence"]
            badge = ('<span class="tip evb {ev}" data-tip="{d}" tabindex="0">{l}</span>'
                     .format(ev=ev, l=funnel.EV_LABEL[ev],
                             d=html.escape(st["detail"], quote=True)))
        dlt = ('<span class="d">−{:,}</span>'.format(st["removed"])
               if st["removed"] else
               ('<span class="d zero">起點</span>' if not i
                else '<span class="d zero">0</span>'))
        rows.append(
            '<div class="wfrow{cls}"><div class="wfname">{nm}{badge}</div>'
            '<div class="wfbar">'
            '<div class="wfkeep{fin}" style="width:{k:.2f}%"></div>'
            '<div class="wfdrop" style="left:{k:.2f}%;width:{d:.2f}%"></div></div>'
            '<div class="wfval"><span class="n">{a:,}</span>{dlt}</div></div>'.format(
                cls=(" total" if last else "") + (" dim" if pending else ""),
                nm=html.escape(st["name"]), badge=badge,
                fin=" final" if last else "", k=keep, d=drop,
                a=st["after"], dlt=dlt))
    return '<div class="wf">{}</div>'.format("".join(rows))



@app.route("/funnel")
def funnel_page():
    p = panel("common", full=True)
    day, dt_ = screens._latest(p)
    prev = screens._prev_close(p, dt_)
    day["prev"] = day["code"].map(prev)
    day["chg_pct"] = (day["close"] / day["prev"] - 1) * 100
    steps, alive = funnel.build(day)
    total = steps[0]["after"]


    # 存活名單：依歷史勝率等級排，同級內依流動性。
    # 不再截斷 —— 這是「通過篩選的完整名單」，截掉一半就不是那個東西了。
    # 頁面長度由前端分頁處理（見 webui.JS），不是靠少給資料。
    alive = alive.sort_values(["tier", "amt20"], ascending=False)
    body_rows = []
    for _, r in alive.iterrows():
        flags = funnel.evidence_row(r)
        # 標籤只放短標題，完整說明掛 tooltip —— 保持表格緊湊
        fl = "".join('<span class="flag tip {c}" data-tip="{d}" tabindex="0">{t}</span>'
                     .format(c=c, t=html.escape(t),
                             d=html.escape("<b>{}</b><br>{}".format(t, d), quote=True))
                     for c, t, d in flags)
        yoy = r.get("yoy")
        yoy_txt = ("—" if yoy is None or pd.isna(yoy)
                   else '<span class="{}">{:+.1f}%</span>'.format(
                       "up" if yoy > 0 else "dn", yoy))
        inst = r.get("法人買超")
        inst_txt = ("—" if inst is None or pd.isna(inst)
                    else '<span class="{}">{:+.0f}%</span>'.format(
                        "up" if inst > 0 else "dn", inst * 100))
        body_rows.append(
            "<tr><td><a href=\"/s/{c}\"><strong>{c}</strong></a></td>"
            "<td><a href=\"/s/{c}\" style=\"color:var(--fg)\">{n}</a>{fl}</td>"
            "<td class=\"sorted\">{t} / 5</td><td>{p:,.2f}</td><td>{g}</td>"
            "<td>{dy}</td><td>{yoy}</td><td>{inst}</td>"
            "<td class=\"mut\">{a:.1f}億</td></tr>".format(
                c=html.escape(str(r["code"])), n=html.escape(str(r["name"])[:9]),
                fl=fl, t=int(r["tier"]) if not pd.isna(r.get("tier")) else 0,
                p=float(r["close"]),
                g='<span class="{}">{:+.2f}%</span>'.format(
                    "up" if r["chg_pct"] > 0 else "dn", r["chg_pct"])
                if not pd.isna(r.get("chg_pct")) else "—",
                dy="{:.2f}%".format(float(r["div_yield"]))
                if not pd.isna(r.get("div_yield")) else "—",
                yoy=yoy_txt, inst=inst_txt, a=float(r["amt20"]) / 1e8))

    tbl = ('<div class="scroll"><table><thead><tr><th>代號</th><th>名稱</th>'
           '<th class="sorted">{tier}</th><th>{c}</th><th>{g}</th>'
           "<th>{dy}</th><th>{yoy}</th><th>{inst}</th><th>{a}</th>"
           "</tr></thead><tbody>{b}</tbody></table></div>".format(
               tier=webui.tip("歷史勝率等級"), c=webui.tip("收盤"),
               g=webui.tip("今日"), dy=webui.tip("殖利率位階", "殖利率位階"),
               yoy=webui.tip("月營收年增"), inst=webui.tip("法人買超"),
               a=webui.tip("20日均額"), b="".join(body_rows)))

    body = """<div class="wrap">
<div class="fnhero">
  <span class="eyebrow">層層剔除 &middot; {asof:%Y/%m/%d} 收盤</span>
  <span class="big">{fin:,}<span class="unit">檔通過</span></span>
  <div class="sub">自 <b>{total:,}</b> 檔上市普通股逐層剔除<span class="sep">&middot;</span>
  淘汰 <b>{pct:.0f}%</b><span class="sep">&middot;</span>剩下的由你自己判斷要不要進場</div>
</div>
{chips}
<div class="card">{wf}</div>
<div class="card">
  <h2>候選名單</h2>
  <p class="lead">依歷史勝率等級排序。名稱旁的標籤是<strong>來源之間的矛盾</strong>，滑過看細節。</p>
  {tbl}
</div>
</div>""".format(total=total, fin=steps[-1]["after"], asof=dt_,
                 pct=(1 - steps[-1]["after"] / max(total, 1)) * 100,
                 chips=_chips(steps), wf=_waterfall(steps, total), tbl=tbl)

    return page("選股 · 候選名單", body, nav="funnel")


def page(title, body, q="", nav=None):
    """webui.page 的薄包裝：自動帶入「這是不是外部訪客」。

    包一層而不是在每個 route 各自傳參數 —— 有 8 個地方呼叫 page()，
    漏掉任何一個就會在那一頁漏出更新按鈕。
    """
    return webui.page(title, body, q=q, nav=nav, remote=is_remote())


def is_remote():
    """這個請求是不是從 Cloudflare Tunnel 進來的（也就是外部訪客）。

    通道會在轉發時補上 CF-Connecting-IP；本機直接開網頁不會有這個標頭。
    不能用 remote_addr 判斷 —— cloudflared 是在同一台機器上把流量送進
    127.0.0.1，所以外部訪客看起來也是本機。
    """
    return bool(request.headers.get("CF-Connecting-IP")
                or request.headers.get("CF-Ray"))


@app.route("/api/update", methods=["POST"])
def api_update():
    # 更新要重建整個倉儲（約 3 分鐘、吃滿 CPU），只有坐在這台機器前面的人
    # 能按。否則把網址分享出去之後，任何人都能讓這台電腦忙上三分鐘。
    if is_remote():
        return jsonify(started=False, error="remote", **_status()), 403
    started = update_async()
    return jsonify(started=started, **_status())


@app.route("/api/update-status")
def api_update_status():
    return jsonify(**_status())


def _status():
    with _upd_lock:
        st = dict(UPD)
    last = data_last_date()
    st["at"] = st["at"].strftime("%H:%M:%S") if st["at"] else None
    st["data_date"] = str(last) if last else None
    st["behind"] = behind_days(last) if last else None
    return st


@app.route("/healthz")
def healthz():
    return jsonify(ok=True)


@app.errorhandler(404)
def nf(e):
    return page("找不到頁面", """<div class="wrap"><div class="empty">
    <h2>找不到這個頁面</h2><div class="note">在上面搜尋股票代號或名稱。</div>
    </div></div>"""), 404


if __name__ == "__main__":
    print("預先載入面板（約 40 秒，只做一次）…", flush=True)
    panel("common")
    panel("etf")
    universe()
    last = data_last_date()
    if last:
        n = behind_days(last)
        print("資料最新 {}（落後 {} 個營業日）".format(last, n), flush=True)
        if n >= 1:
            print("背景更新已啟動，完成後網頁會自動重新載入", flush=True)
    threading.Thread(target=_auto_loop, daemon=True).start()
    print("就緒 -> http://127.0.0.1:5000", flush=True)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
