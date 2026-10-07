"""公開資訊觀測站「重大訊息」：2008 年起每天每一則的發言時間、公司、主旨。第 12 輪研究（排雷）與個股頁用。

資料來源：mopsov.twse.com.tw 的 ajax_t05st02（依日期查全部公司）。2026-10-08 實測：
    - 2008-01-03 查得到 148 則、2010-03-15 342 則，每則有發言日期與時間（到秒）
    - 查「某一天」回傳的是「前一天約 17:30 到這一天 23:59」，相鄰兩天會重疊 ——
      所以每個日曆日都查、再依（日期、時間、代號、主旨）去重，才不會漏
    - 有時間戳，所以可以精準判斷「訊號日收盤前已經公布了什麼」，不會偷看未來

原始檔：raw/mops_news/YYYYMMDD.json.gz（查詢日命名，存解析好的列；查無資料存空陣列，不必重查）
倉儲：mops_news 表（ts, date, code, name, subject），去重後的全部公告。

跑法：
    python mops_news.py              回補 2008-01-01 到今天（冪等，已經抓過的跳過；最近 3 天會重抓）
    python mops_news.py build        只從原始檔重建倉儲的表
"""
import datetime as dt
import gzip
import json
import re
import sys
import time

import duckdb
import pandas as pd
import requests

from config import DB, RAW

URL = "https://mopsov.twse.com.tw/mops/web/ajax_t05st02"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
DIR = RAW / "mops_news"
TABLE = "mops_news"
DELAY = 2.5
START = dt.date(2008, 1, 1)
REFRESH = 3            # 最近幾天每次都重抓（當天還會繼續公告）

_ROW = re.compile(r"(?s)<tr class=['\"]?(?:even|odd)['\"]?[^>]*>(.*?)</tr>")
_TD = re.compile(r"(?s)<td[^>]*>(.*?)</td>")
_TAG = re.compile(r"<[^>]+>")


def _clean(s):
    return re.sub(r"\s+", " ", _TAG.sub("", s).replace("&nbsp;", " ")).strip()


def parse(html):
    """查詢結果的 HTML -> [{date, time, code, name, subject}]。「查無資料」回傳 []；看不懂回傳 None。"""
    if "查無資料" in html:
        return []
    rows = _ROW.findall(html)
    if not rows and "發言日期" not in html:
        return None
    out = []
    for r in rows:
        td = [_clean(c) for c in _TD.findall(r)]
        if len(td) < 5:
            continue
        m = re.match(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", td[0])
        if not m:
            continue
        d = dt.date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
        out.append(dict(date=d.isoformat(), time=td[1], code=td[2], name=td[3], subject=td[4]))
    return out


def fetch(day, tries=4):
    """一個查詢日的全部公告；抓不到回傳 None（下次再試）。"""
    roc = day.year - 1911
    data = dict(encodeURIComponent=1, step=1, step00=0, firstin=1, off=1, TYPEK="all",
                year=roc, month="{:02d}".format(day.month), day="{:02d}".format(day.day))
    for i in range(tries):
        try:
            r = requests.post(URL, data=data, headers={"User-Agent": UA}, timeout=60)
            if r.status_code == 200:
                rows = parse(r.content.decode("utf-8", "replace"))
                if rows is not None:
                    return rows
        except requests.RequestException:
            pass
        time.sleep(DELAY * (2 ** i) + 3)        # 觀測站偶爾回 502，退避重試
    return None


def path(day):
    return DIR / (day.strftime("%Y%m%d") + ".json.gz")


def backfill(start=START, end=None, verbose=True):
    """回補 start ~ end（含）的每個日曆日。新的先抓（個股頁先有得看），舊的後補。回傳 (抓了幾天, 失敗幾天)。"""
    end = end or dt.date.today()
    DIR.mkdir(parents=True, exist_ok=True)
    days = [end - dt.timedelta(days=i) for i in range((end - start).days + 1)]
    recent = {end - dt.timedelta(days=i) for i in range(REFRESH)}
    todo = [d for d in days if d in recent or not path(d).exists()]
    ok = fail = 0
    t0 = time.time()
    for i, d in enumerate(todo):
        rows = fetch(d)
        if rows is None:
            fail += 1
        else:
            path(d).write_bytes(gzip.compress(json.dumps(rows, ensure_ascii=False).encode("utf-8")))
            ok += 1
        if verbose and (i + 1) % 50 == 0:
            el = time.time() - t0
            print("  重大訊息 {}/{}（{}）  失敗 {}  約剩 {:.0f} 分鐘".format(
                i + 1, len(todo), d.isoformat(), fail, el / (i + 1) * (len(todo) - i - 1) / 60), flush=True)
        time.sleep(DELAY)
    if verbose:
        print("重大訊息：抓了 {} 天、失敗 {} 天（失敗的下次會再試）".format(ok, fail), flush=True)
    return ok, fail


def load():
    """全部原始檔 -> 去重後的 DataFrame（ts, date, code, name, subject）。"""
    rows = []
    for p in sorted(DIR.glob("*.json.gz")):
        try:
            rows.extend(json.loads(gzip.decompress(p.read_bytes()).decode("utf-8")))
        except Exception:
            continue
    df = pd.DataFrame(rows, columns=["date", "time", "code", "name", "subject"])
    if df.empty:
        return df.assign(ts=pd.Series(dtype="datetime64[ns]"))
    df = df.drop_duplicates(["date", "time", "code", "subject"])
    df["ts"] = pd.to_datetime(df["date"] + " " + df["time"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)[["ts", "date", "code", "name", "subject"]]


def build(verbose=True):
    df = load()
    con = duckdb.connect(str(DB))
    try:
        con.execute(f"DROP TABLE IF EXISTS {TABLE}")
        con.register("_tmp", df)
        con.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM _tmp")
        con.unregister("_tmp")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    if verbose and len(df):
        print("mops_news：{:,} 則（{} ~ {}）".format(len(df), str(df["ts"].min())[:10], str(df["ts"].max())[:10]), flush=True)
    return len(df)


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    if "build" not in sys.argv[1:]:
        backfill()
    build()
