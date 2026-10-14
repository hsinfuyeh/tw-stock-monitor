"""公開資訊觀測站「重大訊息」：2008 年起每天每一則的發言時間、公司、主旨。第 12 輪研究（排雷）與個股頁用。

資料來源：mopsov.twse.com.tw 的 ajax_t05st02（依日期查全部公司）。2026-10-08 實測：
    - 2008-01-03 查得到 148 則、2010-03-15 342 則，每則有發言日期與時間（到秒）
    - 查「某一天」回傳的是「前一天約 17:30 到這一天 23:59」，相鄰兩天會重疊 ——
      所以每個日曆日都查、再依（日期、時間、代號、主旨）去重，才不會漏
    - 有時間戳，所以可以精準判斷「訊號日收盤前已經公布了什麼」，不會偷看未來

原始檔：raw/mops_news/YYYYMMDD.json.gz（查詢日命名，存解析好的列；查無資料存空陣列，不必重查）
倉儲：mops_news 表（ts, date, code, name, subject），去重後的全部公告。

每天的更新（ci_update.extras）呼叫 update()：只抓最近幾天。

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
    # 沒有公告的日子（多半是星期日、連假）回的是「查無資料」或「查無97/01/13之重大訊息資料」，兩種都是空的一天，
    # 不是抓取失敗。舊版只認前一種，111 天被當成失敗一直重試（2026-10-14 修正）。
    if "查無" in html:
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


def update(verbose=True):
    """每天的更新用：從手上最新一天的前幾天抓到今天（通常 3–4 次請求），再重建表。不會丟例外。"""
    try:
        have = sorted(p.name[:8] for p in DIR.glob("*.json.gz"))
        last = dt.datetime.strptime(have[-1], "%Y%m%d").date() if have else dt.date.today() - dt.timedelta(days=REFRESH)
        start = max(START, last - dt.timedelta(days=REFRESH))
        ok, fail = backfill(start=start, verbose=False)
        n = append([start + dt.timedelta(days=i) for i in range((dt.date.today() - start).days + 1)])
        if verbose:
            print("重大訊息：更新 {} 天（失敗 {}），表裡共 {:,} 則".format(ok, fail, n), flush=True)
    except Exception as e:
        print("::warning::重大訊息更新失敗（不影響網站主線）：{}".format(e), flush=True)


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


def _rows(files):
    rows = []
    for p in files:
        try:
            rows.extend(json.loads(gzip.decompress(p.read_bytes()).decode("utf-8")))
        except Exception:
            continue
    df = pd.DataFrame(rows, columns=["date", "time", "code", "name", "subject"]).drop_duplicates(
        ["date", "time", "code", "subject"])
    df["ts"] = pd.to_datetime(df["date"] + " " + df["time"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df.dropna(subset=["ts"])[["ts", "date", "code", "name", "subject"]]


def append(days):
    """只把這幾個查詢日的公告加進表（已經在表裡的跳過）。每天的更新用，不必每次讀全部 6,800 個檔。
    表還不存在就整個重建。回傳表裡的總筆數。"""
    con = duckdb.connect(str(DB))
    try:
        exists = con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
                             [TABLE]).fetchone()[0]
    finally:
        con.close()
    if not exists:
        return build(verbose=False)
    df = _rows([path(d) for d in days if path(d).exists()])
    con = duckdb.connect(str(DB))
    try:
        if len(df):
            con.register("_tmp", df)
            con.execute(f"""INSERT INTO {TABLE} SELECT * FROM _tmp WHERE NOT EXISTS (
                SELECT 1 FROM {TABLE} t WHERE t.ts = _tmp.ts AND t.code = _tmp.code AND t.subject = _tmp.subject)""")
            con.unregister("_tmp")
            con.execute("CHECKPOINT")
        return con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    finally:
        con.close()


def build(verbose=True):
    """從原始檔整個重建表。原始檔不完整時（例如冷啟動只帶了倉儲、沒帶原始檔）不准把表變小很多。"""
    df = load()
    con = duckdb.connect(str(DB))
    try:
        try:
            old = con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        except Exception:
            old = 0
        if old and len(df) < old * 0.9:
            print("::warning::重大訊息原始檔只有 {:,} 則、表裡有 {:,} 則，原始檔不完整，表不動".format(len(df), old),
                  flush=True)
            return old
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
