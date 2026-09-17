"""把歷史往回補到 2008，但<b>先不動 config</b>。

為什麼要分兩階段 ——

    store.build() 是照 ingest.trading_calendar() 逐日重建的，而那個日曆的
    起點來自 config.BACKFILL_START。如果先把起點改成 2008、回補卻還沒跑完，
    倉儲就會變成「日曆有 2008-2026、但大半檔案不存在」：
    quotes 出現十年的空洞，滾動視窗跨過空洞計算，
    而線上網站會照常顯示那些數字，不會有任何錯誤訊息。

    這個專案已經被「安靜地算錯」咬過五次了，不差這一次。
    所以這支程式只負責把原始檔抓進 raw/，config 等抓完再手動切換。

各資料源實測可回溯到的時間（2026-09 探測）：

    mi_index  行情      2005 起
    margin    融資券    2005 起
    bwibbu    殖利率    2008 起   <- 漏斗的核心排除層，起點受它限制
    t86       三大法人  2013 起   <- 只用在榜單，不是漏斗層，缺了可接受

所以目標起點設 2008：拿得到殖利率，而且涵蓋 2008 金融海嘯、2011、2015、
2018、2020 疫情、2022 空頭 —— 目前的 2019 起點只有 2022 一次真正的空頭。

跑法：
    python backfill_history.py            從 2008 開始補
    python backfill_history.py 2013       改從 2013 開始（連三大法人都要的話）
"""
import datetime as dt
import json
import sys
import time

import ingest
from config import RAW, REQUEST_DELAY

CACHE = RAW / "_calendar_hist.json"


def hist_calendar(start_year, end_ym):
    """用 FMTQIK 建舊年份的交易日曆。逐月抓，結果快取。"""
    if CACHE.exists():
        days = json.loads(CACHE.read_text())
        if days and days[0][:4] <= str(start_year):
            print("沿用既有日曆快取：{} ~ {}，共 {:,} 天".format(
                days[0], days[-1], len(days)), flush=True)
            return days
    days = []
    y, m = start_year, 1
    ey, em = int(end_ym[:4]), int(end_ym[4:6])
    while (y, m) <= (ey, em):
        j = ingest._get("/rwd/zh/afterTrading/FMTQIK",
                        {"date": "{}{:02d}01".format(y, m), "response": "json"})
        if j and j.get("stat") == "OK":
            for r in j.get("data", []):
                yy, mm, dd = r[0].split("/")
                days.append("{:04d}{}{}".format(int(yy) + 1911, mm, dd))
        if m == 1 or m == 7:
            print("  曆 {}-{:02d}  累計 {:,} 天".format(y, m, len(days)), flush=True)
        time.sleep(REQUEST_DELAY)
        m += 1
        if m == 13:
            y, m = y + 1, 1
    days = sorted(set(days))
    CACHE.write_text(json.dumps(days))
    return days


def main(start_year=2008):
    today = dt.date.today()
    # 只補到目前 config 起點之前，之後的已經有了
    from config import BACKFILL_START
    print("目標：{} 年起 ~ {}（現有起點）".format(start_year, BACKFILL_START))
    print("建立歷史交易日曆…", flush=True)
    days = hist_calendar(start_year, today.strftime("%Y%m"))
    days = [d for d in days if d < BACKFILL_START]
    if not days:
        print("沒有要補的日期。")
        return
    todo = [d for d in days
            if not all(ingest.have(ds, d) for ds in ingest.DATASETS)]
    print("\n交易日 {:,} 天（{} ~ {}）".format(len(days), days[0], days[-1]))
    print("其中還沒補齊的 {:,} 天".format(len(todo)))
    est = len(todo) * len(ingest.DATASETS) * REQUEST_DELAY / 3600
    print("預估 {:,} 次請求，約 {:.1f} 小時\n".format(
        len(todo) * len(ingest.DATASETS), est), flush=True)
    if not todo:
        return
    # backfill 本身是冪等的：已經有的檔案會跳過，中斷了直接重跑即可
    ingest.backfill(list(ingest.DATASETS), todo)
    print("\n完成。接下來手動做兩件事才會生效：")
    print("  1. 把 config.py 的 BACKFILL_START 改成 {}0101".format(start_year))
    print("  2. 刪掉 raw/_calendar.json，然後 python -c \"import store; store.build()\"")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2008)
