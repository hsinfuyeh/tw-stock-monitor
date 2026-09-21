"""除權息事件 (TWT49U) — 報酬調整的權威來源。

為什麼一定要這個：
  MI_INDEX 在除權息日只回報 change=0，無法分離「配息」與「當日真實漲跌」。
  台股平均殖利率 3-4%，不調整的話高殖利率股的報酬會被系統性低估，
  而殖利率是價值因子之一 -> 會製造出「高殖利率 = 低報酬」的假訊號。

調整方式（標準向後調整 back-adjustment）：
  除權息日 t：   真實報酬 = close_t / 除權息參考價_t - 1
  其他日：       真實報酬 = close_t / close_{t-1} - 1
"""
import gzip, json, time, datetime as dt
import requests
import pandas as pd
from config import BASE, UA, RAW, REQUEST_DELAY

_s = requests.Session(); _s.headers.update({"User-Agent": UA})
_DIR = RAW / "exrights"; _DIR.mkdir(exist_ok=True)


def _roc_cn(s):
    """115年09月01日 -> 2026-09-01"""
    s = s.strip()
    y = int(s[:s.index("年")]) + 1911
    m = int(s[s.index("年")+1:s.index("月")])
    d = int(s[s.index("月")+1:s.index("日")])
    return f"{y:04d}-{m:02d}-{d:02d}"


def fetch_year(year, max_age_hours=12):
    """抓一年份的除權息事件。

    當年度的檔案不能永久快取 —— 未來的除權息是「陸續公告」的，
    快取起來的話行事曆會停在抓取當下，新公告的永遠看不到。
    所以當年度（含明年，跨年公告）超過 max_age_hours 就重抓。
    """
    p = _DIR / f"{year}.json.gz"
    if p.exists() and p.stat().st_size > 100:
        this_year = dt.date.today().year
        stale = False
        if year >= this_year:
            age_h = (time.time() - p.stat().st_mtime) / 3600
            stale = age_h > max_age_hours
        if not stale:
            return json.loads(gzip.decompress(p.read_bytes()).decode())
    out = []
    # 一年分四季抓，避免單次範圍過大被截斷
    for q in range(4):
        s = dt.date(year, q*3+1, 1)
        e = dt.date(year + (q == 3), (q*3+4) if q < 3 else 1, 1) - dt.timedelta(days=1)
        r = _s.get(BASE + "/rwd/zh/exRight/TWT49U",
                   params={"startDate": s.strftime("%Y%m%d"),
                           "endDate": e.strftime("%Y%m%d"), "response": "json"}, timeout=30)
        try:
            j = r.json()
        except Exception:
            time.sleep(REQUEST_DELAY); continue
        if j.get("stat") == "OK":
            out.extend(j.get("data", []))
        time.sleep(REQUEST_DELAY)
    p.write_bytes(gzip.compress(json.dumps(out, ensure_ascii=False).encode()))
    return out


KINDS = {"權", "息", "權息"}


def _row(r):
    """一列 TWT49U 的資料。<b>欄位位置會隨年份變</b>，所以不能寫死索引。

    2019 之後：… 3 前收 4 參考價 5 權值+息值 6 類型 …
    2008-2018：權值與息值<b>分成兩欄</b>，所以「權值+息值」在 7、類型在 8。

    寫死 r[5]／r[6] 的話，舊年份會把「息值」當成類型（那是數字不是字串），
    解析整個壞掉 —— 2026-09-21 回補到 2008 時就是這樣炸的。
    改成先找「類型」欄（權／息／權息），金額固定是它的前一欄。
    """
    try:
        ki = next(i for i, v in enumerate(r)
                  if isinstance(v, str) and v.strip() in KINDS)
        return dict(date=_roc_cn(r[0]), code=r[1].strip(),
                    prev_close=float(str(r[3]).replace(",", "")),
                    ref_price=float(str(r[4]).replace(",", "")),
                    value=float(str(r[ki - 1]).replace(",", "")),
                    kind=r[ki].strip())
    except (StopIteration, ValueError, IndexError):
        return None


def load(start_year=None, end_year=None):
    """載入除權息事件。預設跟著 config.BACKFILL_START 走。

    起點一定要跟行情資料一致：少了哪一年的除權息，那一年的除息日價格落差
    就會被當成下跌，報酬被系統性低估（台股平均殖利率 3-4%/年）。
    2026-09-21 回補到 2008 之後就踩到這個 —— 行情有 18 年、除權息只有 7 年。
    """
    if start_year is None:
        from config import BACKFILL_START
        start_year = int(str(BACKFILL_START)[:4])
    end_year = end_year or dt.date.today().year
    rows = []
    for y in range(start_year, end_year + 1):
        for r in fetch_year(y):
            one = _row(r)
            if one:
                rows.append(one)
    df = pd.DataFrame(rows)
    if len(df):
        df["date"] = pd.to_datetime(df["date"])
        df = df[(df.ref_price > 0) & (df.prev_close > 0)]
        df = df.drop_duplicates(subset=["date", "code"], keep="last")
    return df


if __name__ == "__main__":
    df = load()
    print(f"除權息事件 {len(df):,} 筆  {df.date.min():%Y-%m-%d} ~ {df.date.max():%Y-%m-%d}")
    print(f"涵蓋 {df.code.nunique():,} 檔證券")
    print("\n類型分布:"); print(df.kind.value_counts().to_string())
    df["yield_pct"] = (df.prev_close - df.ref_price) / df.prev_close * 100
    print(f"\n單次除權息平均調整幅度: {df.yield_pct.mean():.2f}%  中位 {df.yield_pct.median():.2f}%")
    print(f"調整幅度 > 5% 的事件: {(df.yield_pct > 5).sum():,} 筆 "
          f"({(df.yield_pct > 5).mean()*100:.1f}%)")
    import duckdb
    from config import DB
    con = duckdb.connect(str(DB)); con.register("_t", df)
    con.execute("DROP TABLE IF EXISTS exrights"); con.execute("CREATE TABLE exrights AS SELECT * FROM _t")
    con.close(); print("\n已寫入 exrights 表")
