"""本機網站（dashboard.bat 會開這支）。

跑法： python server.py     然後開 http://127.0.0.1:5000

頁面和公開網站是同一份（site/），這裡負責：
  - 供應 site/ 的頁面與資料
  - 資料落後時在背景更新：抓新交易日 -> 重建資料庫 -> 重新產生網頁資料
  - 網頁上「立即更新」與「用我的參數回測」這兩個公開網站做不到的功能
publish.py 也會 import 這支，用它的 panel() / build_checklist() 等資料函式。
"""
import datetime as dt
import json
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, jsonify, redirect, request, send_from_directory

import factors
import likelihood
import stock_report
import store
from config import DB

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
            except Exception as e:
                # 出聲。這裡靜默的話，法人買超／賣超兩個榜單會變成空的，
                # 而畫面上只會顯示「今天沒有符合條件的標的」—— 看起來像
                # 市場沒動靜，實際上是資料沒接上。
                import traceback
                print("::error::三大法人資料載入失敗，相關榜單會是空的：{}".format(e),
                      flush=True)
                traceback.print_exc()
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
            except Exception as e:
                # 這是整個系統最不能靜默的地方。div_yield 全缺值 -> ypct 全缺值
                # -> 漏斗的「殖利率位階過低」那層刷掉 0 檔 -> 候選名單暴增，
                # 而且畫面上完全看不出異常。月營收那次就是這個劇本，
                # 只是從另一個入口進來的。
                import traceback
                print("::error::估值資料載入失敗，殖利率排除規則會整層失效：{}"
                      .format(e), flush=True)
                traceback.print_exc()
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
                    d = d.merge(
                        pit[["date", "code", "yoy", "yoy_3m", "sue", "days_since"]],
                        on=["date", "code"], how="left")
                    n = int(d["yoy"].notna().sum())
                    print("  月營收：{:,} 筆原始、面板覆蓋 {:,} 列".format(len(rv), n),
                          flush=True)
                    # ETF 沒有月營收，覆蓋 0 列是正常的，不該報警 ——
                    # 會誤報的警告最後只會訓練人忽略所有警告。
                    if not n and kind == "common":
                        print("::warning::月營收讀到了但一列都對不上面板 —— "
                              "L3 營收層會整層失效。", flush=True)
                else:
                    print("::warning::revenue.load() 回傳空的，L3 營收層將失效。",
                          flush=True)
                    d["yoy"] = d["yoy_3m"] = d["sue"] = d["days_since"] = np.nan
            except Exception as e:
                import traceback
                print("::error::月營收載入失敗，L3 營收層將失效：{}".format(e),
                      flush=True)
                traceback.print_exc()
                d["yoy"] = d["yoy_3m"] = d["sue"] = d["days_since"] = np.nan
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
        _set(msg="產生網頁資料…", detail="約需 4 分鐘")
        publish_site()

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
        lv, t = "ok", "成交量充足"
        d = "近 20 日平均每天成交 {:.1f} 億元。一般散戶的買賣不會有問題。".format(amt / 1e8)
    elif amt >= 1e8:
        lv, t = "ok", "成交量正常"
        d = "近 20 日平均每天成交 {:.1f} 億元。單筆金額控制在它的 1% 以內較安全。".format(amt / 1e8)
    elif amt >= 2e7:
        lv, t = "warn", "成交量偏低"
        d = ("近 20 日平均每天只成交 {:.2f} 億元。大單進出會推動價格，"
             "而且急著賣時可能賣不到理想價位。".format(amt / 1e8))
    else:
        lv, t = "bad", "成交量太小"
        d = ("近 20 日平均每天只成交 {:.2f} 億元。這麼小的量很難順利買賣，"
             "看到的價格也常常只是零星成交。".format(amt / 1e8))
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
        d = "一年波動幅度 {:.0f}%，每天常見漲跌約 ±{:.1f}%。".format(sd, daily)
    elif pct < 35:
        lv, t = "ok", "波動低於多數股票"
        d = ("一年波動幅度 {:.0f}%，每天常見漲跌約 ±{:.1f}%。"
             "只比市場上 {:.0f}% 的股票波動大，相對穩定。".format(sd, daily, pct))
    elif pct < 70:
        lv, t = "ok", "波動中等"
        d = ("一年波動幅度 {:.0f}%，每天常見漲跌約 ±{:.1f}%。"
             "比市場上 {:.0f}% 的股票波動大，屬於中間。".format(sd, daily, pct))
    elif pct < 90:
        lv, t = "warn", "波動偏高"
        d = ("一年波動幅度 {:.0f}%，每天常見漲跌約 ±{:.1f}%。"
             "比市場上 {:.0f}% 的股票波動大，買的金額要少一點。".format(sd, daily, pct))
    else:
        lv, t = "bad", "波動很高"
        d = ("一年波動幅度 {:.0f}%，每天常見漲跌約 ±{:.1f}%。"
             "比市場上 {:.0f}% 的股票波動大 —— 買同樣金額，"
             "風險是一般股票的好幾倍。".format(sd, daily, pct))
    items.append((lv, t, d, "一年波動幅度"))

    # 3. 近期漲跌停 —— 鎖死時買不到也賣不掉
    nlim = last.get("limit_20d")
    nlim = 0 if nlim is None or pd.isna(nlim) else int(nlim)
    if nlim == 0:
        items.append(("ok", "近期無漲跌停",
                      "近 20 個交易日沒有觸及漲跌停，交易狀態正常。", "漲跌停"))
    else:
        items.append(("warn", "近期曾觸及漲跌停",
                      "近 20 個交易日碰到 {} 次。整天鎖在漲停或跌停時買不到也賣不掉，"
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
            rank_txt = "（比全市場 {:.0f}% 的股票高）".format(float(ppct) * 100)
            if float(ppct) < 0.30:
                lv = "bad"
        if lv == "bad":
            extra = ("<strong>落在全市場最低 30%。</strong>"
                     "過去統計，排除這一群之後，剩下的股票平均每年多賺約 2.9%。")
        elif pos >= 90:
            extra = "接近一年來的高點。這不代表該賣，但回檔空間比較大。"
        else:
            extra = "位於區間中上段。"
        items.append((lv, "在一年高低點之間的位置",
                      "目前在近一年高低區間的 {:.0f}%（0% 是一年最低、100% 是最高）{}。{}".format(pos, rank_txt, extra),
                      "一年區間位置"))

    # 6. 殖利率位階 —— 排除最低 40% 可讓剩餘池子的「中位數」+5.49%/年
    ypct, dy = last.get("ypct"), last.get("div_yield")
    if ypct is not None and not pd.isna(ypct):
        if float(ypct) < 0.40:
            items.append(("bad", "殖利率偏低",
                          "殖利率 {:.2f}%，只比全市場 {:.0f}% 的股票高。"
                          "<strong>落在最低 40%。</strong>"
                          "過去統計，排除這一群之後，剩下的股票一般情況下每年多賺約 5.5%。".format(
                              float(dy) if dy == dy else 0, float(ypct) * 100),
                          "殖利率排名"))
        else:
            items.append(("ok", "殖利率正常",
                          "殖利率 {:.2f}%，比全市場 {:.0f}% 的股票高。".format(
                              float(dy) if dy == dy else 0, float(ypct) * 100),
                          "殖利率排名"))
    return items


TIER_TXT = {
    5: ("ok", "過去條件相似的股票，<b>53.6%</b> 在之後 10 天表現贏過市場上一半的股票"),
    4: ("ok", "過去條件相似的股票，<b>51.3%</b> 在之後 10 天表現贏過市場上一半的股票"),
    3: ("", "過去條件相似的股票，<b>50.4%</b> 在之後 10 天表現贏過市場上一半的股票"),
    2: ("warn", "過去條件相似的股票，<b>48.3%</b> 在之後 10 天表現贏過市場上一半的股票"),
    1: ("warn", "過去條件相似的股票，<b>46.2%</b> 在之後 10 天表現贏過市場上一半的股票"),
}


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
# 通過 t>3.0 的榜單（「其他排行」頁的「過去表現較好」組）。
SIGNAL_KEYS = ("sue", "pos", "rev")

# 「其他排行」頁的分組。組名要讓一般人一眼看懂這組在講什麼。
TAB_GROUPS_SPEC = [
    ("過去表現較好", ["sue", "pos", "rev"]),
    ("今日行情", ["amount", "gain", "loss", "volume", "high"]),
    ("法人動向", ["inst", "instout"]),
    ("其他", ["calm", "exdiv"]),
    ("過去表現較差", ["low", "score"]),
]
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


# --------------------------------------------------------------------- 網頁
# 本機和公開網站用同一份頁面（site/）。以前本機是 Flask 自己組 HTML，
# 兩邊各一份，改一個字要改兩次，總有一邊會忘 —— 所以只留一份。
# 本機多兩件公開網站做不到的事：按一下立即更新、用自訂參數回測。
SITE = Path(__file__).parent / "site"
_short = {}


def _short_features():
    """整段歷史的特徵面板，倉儲沒變就不重算（約 20 秒）。"""
    import shortterm
    m = DB.stat().st_mtime
    if _short.get("mtime") != m:
        with _lock:
            _short.update(d=shortterm.features(), mtime=m)
    return _short["d"]


def site_data_date():
    """site/data 是用哪一天的資料產生的。"""
    try:
        with open(SITE / "data" / "meta.json", encoding="utf-8") as f:
            return json.load(f).get("data_date")
    except Exception:
        return None


def publish_site():
    """重新產生 site/data（約 4 分鐘）。失敗就讓例外往上丟，由呼叫端標成更新失敗。"""
    import publish
    publish.build()


def publish_async():
    """倉儲已是最新、但網頁資料比較舊（例如第一次在這台電腦開）時用。"""
    with _upd_lock:
        if UPD["state"] == "running":
            return False
        UPD.update(state="running", msg="產生網頁資料…", detail="約需 4 分鐘",
                   at=dt.datetime.now())

    def run():
        try:
            publish_site()
            _set(state="done", msg="網頁資料已更新", detail="", at=dt.datetime.now())
        except Exception as e:
            _set(state="error", msg="網頁資料產生失敗", detail=str(e)[:200],
                 at=dt.datetime.now())
    threading.Thread(target=run, daemon=True).start()
    return True


@app.route("/")
def home():
    return send_from_directory(SITE, "index.html")


@app.route("/<name>.html")
def site_page(name):
    return send_from_directory(SITE, name + ".html")


@app.route("/assets/<path:p>")
def site_assets(p):
    return send_from_directory(SITE / "assets", p)


@app.route("/data/<path:p>")
def site_data(p):
    r = send_from_directory(SITE / "data", p)
    r.headers["Cache-Control"] = "no-cache"      # 更新後馬上看得到
    return r


# 舊網址（書籤）轉到新頁面
@app.route("/s/<code>")
def _old_stock(code):
    return redirect("/stock.html?c={}".format(code))


@app.route("/lists")
@app.route("/lists/<key>")
def _old_lists(key="amount"):
    return redirect("/lists.html?s={}".format(key))


@app.route("/funnel")
def _old_funnel():
    return redirect("/funnel.html")


@app.route("/short")
def _old_short():
    return redirect("/short.html")


@app.route("/api/short/backtest", methods=["POST"])
def api_short_backtest():
    # 整段歷史回測約 10 秒 CPU，跟更新一樣只給本機
    if is_remote():
        return jsonify(error="remote"), 403
    import shortterm
    p = shortterm.params(**(request.get_json(silent=True) or {}))
    w = shortterm.windows(_short_features(), p)
    return jsonify(recent=w["recent"][1], all=w["all"][1], params=p)


@app.route("/healthz")
def healthz():
    return jsonify(ok=True)


if __name__ == "__main__":
    # publish.py 會 import server；不讓它再載一份（那份會有自己的面板快取與鎖）
    import sys
    sys.modules.setdefault("server", sys.modules["__main__"])
    last = data_last_date()
    if last:
        n = behind_days(last)
        print("資料最新 {}（落後 {} 個營業日）".format(last, n), flush=True)
        if n >= 1:
            print("背景更新已啟動，完成後網頁會自動重新載入", flush=True)
        elif site_data_date() != str(last):
            print("網頁資料比倉儲舊，背景重新產生（約 4 分鐘）", flush=True)
            publish_async()
    threading.Thread(target=_auto_loop, daemon=True).start()
    print("就緒 -> http://127.0.0.1:5000", flush=True)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
