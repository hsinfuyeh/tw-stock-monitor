"""處置股與注意股名單（規範 v1 第 4 節排除規則 1）。

為什麼要排除：處置股改成分盤集合競價（每隔幾分鐘才撮合一次）、還可能要預收款券，
停損停利不一定按你的價位成交；注意股是交易所已經點名「異常」的，
下一步常常就是處置。這條規則不靠預測力，是執行面的常理（規範等級 B）。

資料來源（TWSE 公開資料，每天收盤後公布）：
  處置股  /rwd/zh/announcement/punish   目前還在處置期間的
  注意股  /rwd/zh/announcement/notice   指定日期當天被公布的

**抓不到時一定要讓畫面知道**。名單抓失敗如果當成「今天沒有處置股」，
等於規則安靜地失效 —— 這個專案最常出事的就是這種情況。所以回傳一律帶 status：
  ok           抓到了
  unavailable  抓不到（網路、TWSE 限流），頁面要提醒你自己查
注意股在收盤後才陸續公布，產出網頁時可能還沒出來 —— 所以 0 筆時標成
pending（可能還沒公布），不當成確定沒有。

全額交割股沒有找到穩定的結構化來源，頁面上請你自己確認。
"""
import datetime as dt
import gzip
import json
import time

import requests

from config import BASE, RAW, UA

DIR = RAW / "risklists"


def _roc(s):
    """115/09/22 或 115.09.22 -> 2026-09-22"""
    s = s.strip().replace(".", "/")
    y, m, d = s.split("/")
    return "{:04d}-{:02d}-{:02d}".format(int(y) + 1911, int(m), int(d))


def _get(path, params, retries=2):
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    last = None
    for i in range(retries + 1):
        try:
            r = s.get(BASE + path, params=dict(params, response="json"), timeout=30)
            j = r.json()
            if j.get("stat") == "OK" or j.get("stat") is not None:
                return j
        except Exception as e:           # 網路或解析錯誤：重試，最後才放棄
            last = e
        time.sleep(3 * (i + 1))
    raise RuntimeError("TWSE {} 抓不到：{}".format(path, last))


def parse_punish(j, date):
    """處置期間涵蓋 date 的處置股。欄位用名稱找，不寫死位置。"""
    f = j.get("fields") or []
    try:
        ic, ip = f.index("證券代號"), f.index("處置起迄時間")
    except ValueError:
        return []
    out = []
    for row in j.get("data") or []:
        try:
            a, b = [x.strip() for x in str(row[ip]).replace("~", "～").split("～")]
            if _roc(a) <= date <= _roc(b):
                out.append(str(row[ic]).strip())
        except (ValueError, IndexError):
            continue
    return sorted(set(out))


def parse_notice(j):
    f = j.get("fields") or []
    try:
        ic = f.index("證券代號")
    except ValueError:
        return []
    return sorted({str(r[ic]).strip() for r in (j.get("data") or []) if len(r) > ic})


def fetch(date, use_cache=True):
    """date 當天的處置股與注意股。date 是 YYYY-MM-DD（資料日）。

    處置股的查詢只給「現在」的名單，所以只對最新一天有意義；
    補算舊日子時處置股標成 unknown，不假裝有資料。
    """
    DIR.mkdir(parents=True, exist_ok=True)
    cache = DIR / "{}.json.gz".format(date)
    if use_cache and cache.exists():
        got = json.loads(gzip.decompress(cache.read_bytes()).decode())
        if got.get("notice_status") == "ok":      # 注意股已確定，不用再抓
            return got
    out = {"date": date, "fetched_at": dt.datetime.now().isoformat(timespec="seconds")}
    ymd = date.replace("-", "")
    try:
        out["disposal"] = parse_punish(_get("/rwd/zh/announcement/punish", {}), date)
        out["disposal_status"] = "ok"
    except Exception as e:
        out["disposal"], out["disposal_status"], out["disposal_error"] = [], "unavailable", str(e)[:200]
    time.sleep(2)
    try:
        j = _get("/rwd/zh/announcement/notice",
                 {"querytype": "1", "startDate": ymd, "endDate": ymd})
        out["attention"] = parse_notice(j)
        # 0 筆多半是還沒公布，不是真的沒有
        out["notice_status"] = "ok" if out["attention"] else "pending"
    except Exception as e:
        out["attention"], out["notice_status"], out["notice_error"] = [], "unavailable", str(e)[:200]
    cache.write_bytes(gzip.compress(json.dumps(out, ensure_ascii=False).encode()))
    return out


def unknown(date):
    """補算的舊日子：沒有當時的名單，照實標成 unknown。"""
    return {"date": date, "disposal": [], "attention": [],
            "disposal_status": "unknown", "notice_status": "unknown"}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    d = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    print(json.dumps(fetch(d, use_cache=False), ensure_ascii=False, indent=1))
