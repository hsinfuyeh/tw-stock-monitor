"""L1 擷取層 — 每日全市場快照。

設計原則：
  * 冪等：已存在且有效的檔案直接跳過，可以隨時中斷續跑。
  * 不可變歸檔：原始 JSON 原封不動 gzip 存檔，解析錯了不必重抓。
  * universe 從每日快照建構，不從「今天的清單」回補 -> 避免存活者偏誤。
"""
import gzip, json, sys, time
import requests
from config import BASE, UA, RAW, DATASETS, REQUEST_DELAY, MAX_RETRY, BACKFILL_START

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


def _get(path, params):
    for attempt in range(MAX_RETRY):
        try:
            r = _s.get(BASE + path, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 503):
                time.sleep(REQUEST_DELAY * (2 ** attempt) + 5)
                continue
            return None
        except Exception:
            time.sleep(REQUEST_DELAY * (2 ** attempt))
    return None


def trading_calendar(start=BACKFILL_START, end=None):
    """用 FMTQIK（每月市場統計）建交易日曆 — 每月 1 次請求就能拿到該月所有交易日。
    絕對不要用「連續日期」去推，休市日會全部算進去。"""
    import datetime as dt
    cache = RAW / "_calendar.json"
    end = end or dt.date.today().strftime("%Y%m%d")
    cur_ym = end[:6]

    # 已結束的月份不會再變，可以永久快取；<b>當月一定要重抓</b>，因為當月的
    # 交易日還在一天一天長出來。
    #
    # 舊版是無條件回傳整份快取，造成一個完全安靜的故障：日曆永遠停在建立當天，
    # 而 run.py daily 是拿 trading_calendar()[-15:] 決定要抓哪些日子的 ——
    # 於是每天都「成功」跑完、沒有任何錯誤訊息，但資料永遠不會前進。
    # 沒有噴錯的停滯比噴錯的失敗危險得多，因為沒有人會發現。
    days = []
    if cache.exists():
        try:
            days = [d for d in json.loads(cache.read_text()) if d[:6] < cur_ym]
        except Exception:
            days = []
    have = {d[:6] for d in days}

    y, m = int(start[:4]), int(start[4:6])
    ey, em = int(end[:4]), int(end[4:6])
    while (y, m) <= (ey, em):
        ym = f"{y}{m:02d}"
        if ym not in have:
            j = _get("/rwd/zh/afterTrading/FMTQIK",
                     {"date": f"{ym}01", "response": "json"})
            if j and j.get("stat") == "OK":
                for r in j.get("data", []):
                    yy, mm, dd = r[0].split("/")
                    days.append(f"{int(yy)+1911:04d}{mm}{dd}")
            print(f"  曆: {y}-{m:02d} 累計 {len(days)} 交易日", flush=True)
            time.sleep(REQUEST_DELAY)
        m += 1
        if m == 13: y, m = y + 1, 1
    days = sorted(set(d for d in days if start <= d <= end))
    cache.write_text(json.dumps(days))
    return days


def path_for(ds, date):
    d = RAW / ds
    d.mkdir(exist_ok=True)
    return d / f"{date}.json.gz"


def have(ds, date):
    p = path_for(ds, date)
    return p.exists() and p.stat().st_size > 200


def fetch_day(ds, date):
    """抓一個資料集的一天。回傳 True 表示檔案已就緒。"""
    if have(ds, date):
        return True
    cfg = DATASETS[ds]
    params = dict(cfg["params"]); params.update({"date": date, "response": "json"})
    j = _get(cfg["path"], params)
    if j is None:
        return False
    # stat 不是 OK 通常代表當天該資料集無資料（休市、該表當日未產出）
    # 一樣存檔，避免每次重跑都重抓。
    path_for(ds, date).write_bytes(gzip.compress(json.dumps(j, ensure_ascii=False).encode()))
    return True


def load_raw(ds, date):
    p = path_for(ds, date)
    if not p.exists(): return None
    try:
        return json.loads(gzip.decompress(p.read_bytes()).decode())
    except Exception:
        return None


def backfill(datasets, days):
    total = len(days) * len(datasets)
    done = skipped = failed = 0
    t0 = time.time()
    for i, date in enumerate(days):
        for ds in datasets:
            if have(ds, date):
                skipped += 1; continue
            ok = fetch_day(ds, date)
            done += 1 if ok else 0
            failed += 0 if ok else 1
            time.sleep(REQUEST_DELAY)
        if (i + 1) % 25 == 0:
            el = time.time() - t0
            rate = done / el if el else 0
            left = (total - done - skipped) / rate if rate else 0
            print(f"  [{i+1}/{len(days)}] 抓取 {done} 跳過 {skipped} 失敗 {failed} "
                  f"| 剩餘約 {left/60:.0f} 分", flush=True)
    print(f"完成: 抓取 {done} 跳過 {skipped} 失敗 {failed}", flush=True)


if __name__ == "__main__":
    ds_list = sys.argv[1].split(",") if len(sys.argv) > 1 else list(DATASETS)
    print("建立交易日曆…", flush=True)
    days = trading_calendar()
    print(f"交易日 {len(days)} 天: {days[0]} ~ {days[-1]}", flush=True)
    if len(sys.argv) > 2:
        days = days[-int(sys.argv[2]):]
        print(f"限定最近 {len(days)} 天", flush=True)
    backfill(ds_list, days)
