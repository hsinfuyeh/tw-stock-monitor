"""月營收擷取（FinMind）。

為什麼需要這個 —— 它是「剔除價值陷阱」那一層的核心：

    殖利率 = 股利 / 股價。股價暴跌會讓殖利率飆高。
    我的統計層會把這種股票評為高分，但那不是便宜，是公司在爛掉。
    加上營收趨勢才分得出「便宜的好公司」與「價值陷阱」。

可用日（avail_date）—— 這筆營收何時才真的看得到：

    月營收依規定在次月 10 日前公布。若直接用 revenue_year/month 對應報酬，
    等於在營收還沒公布時就知道了它 —— 那是 look-ahead bias。

    FinMind 有 create_time 欄位，但實測只有最近幾筆有值，歷史全是空字串，
    所以不能拿來當回測依據。改用法規期限推算：

        可用日 = 次月 10 日之後的第一個交易日，再往後推 1 個交易日

    多推一天是刻意的保守緩衝。實測有公司晚到 13 日才公布（假日順延），
    寧可晚一天看到資料，也不要在回測裡拿到當時不存在的資訊 ——
    偏保守只會低估效果，偏樂觀則會製造假的績效。

速率限制：FinMind 免費版無 token 300 次/小時，註冊取得 token 後 600 次/小時。
逐檔抓，所以做了本地快取 —— 已抓過的月份不會重抓。
"""
import gzip
import json
import os
import time

import pandas as pd
import requests

from config import RAW

API = "https://api.finmindtrade.com/api/v4/data"
DIR = RAW / "revenue"
DIR.mkdir(exist_ok=True)

# 沒 token 是 300/hr，設 12 秒間隔 -> 每小時 300 次，剛好貼齊上限。
# 有 token（環境變數 FINMIND_TOKEN）可設 6 秒 -> 600/hr。
TOKEN = os.environ.get("FINMIND_TOKEN", "")
DELAY = 6.0 if TOKEN else 12.0


def _path(code):
    return DIR / "{}.json.gz".format(code)


def have(code):
    p = _path(code)
    return p.exists() and p.stat().st_size > 60


def fetch_one(code, start="2018-01-01", retries=3):
    """抓單一檔的月營收。已有快取就直接回傳。"""
    if have(code):
        try:
            return json.loads(gzip.decompress(_path(code).read_bytes()).decode())
        except Exception:
            pass
    params = {"dataset": "TaiwanStockMonthRevenue", "data_id": code,
              "start_date": start}
    if TOKEN:
        params["token"] = TOKEN
    for attempt in range(retries):
        try:
            r = requests.get(API, params=params, timeout=45)
            if r.status_code == 402 or "limit" in r.text.lower()[:200]:
                # 撞到額度，等久一點再試
                time.sleep(60 * (attempt + 1))
                continue
            j = r.json()
            if j.get("status") == 200:
                data = j.get("data") or []
                _path(code).write_bytes(
                    gzip.compress(json.dumps(data, ensure_ascii=False).encode()))
                return data
            return []
        except Exception:
            time.sleep(5 * (attempt + 1))
    return []


def backfill(codes, verbose=True):
    """批次抓取，冪等可中斷續跑。"""
    todo = [c for c in codes if not have(c)]
    if verbose:
        print("月營收：需抓 {} / {} 檔（已快取 {}）".format(
            len(todo), len(codes), len(codes) - len(todo)), flush=True)
    t0 = time.time()
    for i, c in enumerate(todo):
        fetch_one(c)
        time.sleep(DELAY)
        if verbose and (i + 1) % 20 == 0:
            el = time.time() - t0
            left = (len(todo) - i - 1) * (el / (i + 1))
            print("  [{}/{}] 剩餘約 {:.0f} 分".format(i + 1, len(todo), left / 60),
                  flush=True)
    if verbose:
        print("完成，共 {} 檔有資料".format(sum(1 for c in codes if have(c))), flush=True)


def avail_dates(rev_months, trading_days):
    """把「營收所屬月份」換算成「可用日」。

    次月 10 日 -> 之後第一個交易日 -> 再推 1 個交易日（保守緩衝）。
    """
    td = pd.DatetimeIndex(sorted(pd.unique(trading_days)))
    deadline = (pd.DatetimeIndex(rev_months) + pd.DateOffset(months=1)).map(
        lambda d: d.replace(day=10))
    idx = td.searchsorted(deadline, side="left") + 1     # +1 = 緩衝一個交易日
    idx = idx.clip(0, len(td) - 1)
    out = td[idx]
    # 超出交易日曆範圍的（最新一筆營收可能還沒對應的交易日）標為缺值
    return pd.Series(out).where(pd.Series(deadline) <= td[-1], pd.NaT)


def load(codes=None):
    """載入所有快取的月營收，回傳長表。

    產出欄位：
        code            股票代號
        avail_date      可用日（依法規期限推算，見模組說明）
        rev_month       營收所屬年月
        revenue         當月營收
        yoy             年增率（與去年同月比）
        mom             月增率
        yoy_3m          近 3 個月營收合計的年增率（平滑單月波動）
    """
    import ingest
    cal = pd.to_datetime([
        "{}-{}-{}".format(x[:4], x[4:6], x[6:]) for x in ingest.trading_calendar()])
    rows = []
    files = ([_path(c) for c in codes] if codes else sorted(DIR.glob("*.json.gz")))
    for p in files:
        if not p.exists():
            continue
        try:
            data = json.loads(gzip.decompress(p.read_bytes()).decode())
        except Exception:
            continue
        for r in data:
            rows.append((r["stock_id"], r.get("create_time"),
                         r["revenue_year"], r["revenue_month"], r["revenue"]))
    if not rows:
        return pd.DataFrame()
    d = pd.DataFrame(rows, columns=["code", "create_time", "y", "m", "revenue"])
    d["rev_month"] = pd.to_datetime(
        d["y"].astype(str) + "-" + d["m"].astype(str).str.zfill(2) + "-01")
    d = d.sort_values(["code", "rev_month"]).drop_duplicates(
        subset=["code", "rev_month"], keep="last").reset_index(drop=True)
    d["avail_date"] = avail_dates(d["rev_month"], cal)
    d = d[d["avail_date"].notna()].reset_index(drop=True)
    g = d.groupby("code", sort=False)["revenue"]
    d["yoy"] = g.pct_change(12) * 100
    d["mom"] = g.pct_change(1) * 100
    r3 = d.groupby("code", sort=False)["revenue"].transform(
        lambda s: s.rolling(3).sum())
    d["yoy_3m"] = (r3 / r3.groupby(d["code"]).shift(12) - 1) * 100
    return d[["code", "avail_date", "rev_month", "revenue", "yoy", "mom", "yoy_3m"]]


def as_of_panel(rev, dates):
    """把月營收攤成「每個交易日當下可知的最新一筆」。

    這是 point-in-time 的關鍵：某個交易日看得到的，只有 avail_date <= 該日
    的那些營收。用 merge_asof 依 avail_date 對齊，天然不會偷看未來。
    """
    if not len(rev):
        return pd.DataFrame()
    out = []
    dates = pd.DatetimeIndex(sorted(pd.unique(dates)))
    for code, g in rev.groupby("code", sort=False):
        g = g.sort_values("avail_date")
        left = pd.DataFrame({"date": dates})
        m = pd.merge_asof(left, g[["avail_date", "yoy", "yoy_3m", "mom", "rev_month"]],
                          left_on="date", right_on="avail_date", direction="backward")
        m["code"] = code
        out.append(m)
    r = pd.concat(out, ignore_index=True)
    # 營收太舊（超過 75 天沒更新）視為缺值，避免停止公布的公司留著舊數字
    r.loc[(r["date"] - r["avail_date"]).dt.days > 75,
          ["yoy", "yoy_3m", "mom"]] = pd.NA
    return r[["date", "code", "yoy", "yoy_3m", "mom", "avail_date", "rev_month"]]


if __name__ == "__main__":
    import sys
    import store
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    # 只抓流動性最好的 N 檔 —— 那本來就是實際可交易的範圍
    liq = store.q("""
        SELECT code FROM quotes
        WHERE date >= (SELECT MAX(date) - INTERVAL 30 DAY FROM quotes)
          AND cat = 'common'
        GROUP BY code HAVING AVG(amount) >= 20000000
        ORDER BY AVG(amount) DESC LIMIT {}""".format(n))
    codes = list(liq["code"])
    print("目標 {} 檔（依 20 日均額排序）".format(len(codes)), flush=True)
    backfill(codes)
