"""CI 用的資料更新：抓缺的交易日、檢查齊備、重建倉儲。

跟 server.py 裡那個背景更新是同一套邏輯，差別在於這裡沒有網頁可以顯示進度，
所以把判斷結果寫成 ci_summary.md 給 GitHub 的執行摘要看。

<b>資料齊備閘門</b>是這支程式存在的主要理由 ——

    TWSE 各資料源的發布時間不一樣：行情（MI_INDEX）約 14:30 就出來，
    估值（BWIBBU）與三大法人（T86）更晚，融資券（MI_MARGN）最晚。
    所以每天下午都有一段時間「行情有了、估值還沒有」。

    本機版曾經真的中招：那天的 div_yield 全是缺值，殖利率那層排除規則
    靜默失效，候選名單從 221 檔虛胖成 407 檔，而畫面上沒有任何跡象。

    在 CI 裡後果更嚴重 —— 那份虛胖的名單會<b>直接發佈到公開網站</b>。
    所以三張核心表沒有同一天的資料就不發佈，直接以成功狀態結束，
    晚一點再按一次即可。寧可晚幾小時，也不要publish 一份半套的名單。
"""
import os
import sys

import ingest
import store

# 面板會用到的三個資料源。margin（融資券）不在內，因為 panel 沒有合併它，
# 而且它是最晚發布的 —— 把它算進閘門會讓下午幾乎永遠過不了。
CORE = ("mi_index", "bwibbu", "t86")
LOOKBACK = 20          # 只檢查最近幾個交易日，不必每次掃全部
SUMMARY = "ci_summary.md"

# 上櫃與主動式 ETF（只供查詢）的抓取量。平常的更新只顧新的日子：
# 舊版每次都順便補 15 天上櫃歷史、40 個 ETF 歷史請求，當天第一次更新因此要 12–14 分鐘，
# 而沒有定時排程之後，等的是第一個打開網頁的人。歷史改由「補歷史」模式（workflow 的
# backfill 選項，BACKFILL=true）另外跑：時間用秒數限制，job 上限 60 分鐘、產出與存快取約 10 分鐘。
DAILY_OTC_DAYS = 3     # 幾天沒人更新也補得回來；平常只會缺當天 1 天
DAILY_ETF_REQ = 6
BACKFILL_OTC_SEC = 28 * 60
BACKFILL_ETF_SEC = 8 * 60


def _d(v):
    """日期只顯示到日，不要拖著 00:00:00 的尾巴。"""
    return str(v)[:10] if v is not None else "（無）"


def log(msg):
    print(msg, flush=True)


def write_summary(lines):
    with open(SUMMARY, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def set_output(key, value):
    """把結果交給 workflow 決定要不要繼續。

    不能只靠離開碼：閘門擋下時是「刻意不發佈」而不是失敗，用失敗狀態會讓人
    習慣性忽略紅色叉叉；但若單純回 0，workflow 又會照常往下 publish ——
    那正好是閘門要阻止的事。所以用一個明確的輸出旗標，兩邊語意才對得起來。
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("{}={}\n".format(key, value))
    log("[output] {}={}".format(key, value))


def backfill_mode():
    """這次是「補歷史」嗎（workflow 的 backfill 選項）。"""
    return os.environ.get("BACKFILL", "").lower() == "true"


def extras():
    """上櫃與主動式 ETF（只供查詢）。不在閘門裡：它們是附加資訊，缺了不該擋住發佈；
    兩個 update 都不會丟例外。回傳上櫃寫入的交易日數。"""
    import tpex
    import activeetf
    if backfill_mode():
        # 上櫃要 60 天才產得出個股頁（算波動），目標 300 天；櫃買約 20 秒一天
        n_otc = tpex.update(seconds=BACKFILL_OTC_SEC)
        activeetf.update(budget=None, seconds=BACKFILL_ETF_SEC)
    else:
        n_otc = tpex.update(budget=DAILY_OTC_DAYS)
        activeetf.update(budget=DAILY_ETF_REQ)
    log("上櫃寫入 {} 個交易日".format(n_otc))
    return n_otc


def automatic():
    """這次是自動觸發的嗎：網頁發現資料過期而觸發（trigger=cron），或舊的 GitHub 排程。

    沒有定時排程了（2026-10-01 起），但「自動觸發」的規則照舊：證交所還沒發完就安靜略過、
    當天已發佈過就不重做 —— 開網頁的人很多，不能每個人都叫它重做一次。按 ⟳ 的（manual）照樣發佈。"""
    return (os.environ.get("GITHUB_EVENT_NAME") == "schedule"
            or os.environ.get("TRIGGER") == "cron")


def usable_last_date():
    """三張核心表都有資料的最後一天。"""
    try:
        cols = ", ".join("(SELECT MAX(date) FROM {0}) AS {0}".format(t)
                         for t in ("quotes", "inst", "valuation"))
        r = store.q("SELECT " + cols)
        vals = [r[t][0] for t in ("quotes", "inst", "valuation")]
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else None
    except Exception:
        return None


def check_revenue():
    """月營收資料在不在、新不新。回傳 (月份數, 最新月份 'YYYYMM' 或 None)。

    這一項不在上面的三張表裡：月營收是 publish 時由 revenue.load() 從
    raw/revenue_mops/ 直接讀的，不進 DuckDB。所以倉儲看起來完全正常，
    但只要那個資料夾沒跟著過來，yoy 就全是缺值，漏斗的 L3 營收層
    <b>整層失效</b> —— 候選名單從 221 檔虛胖成 263 檔，沒有任何錯誤訊息。

    第一次部署上線就是這樣中的：閘門只檢查三個 TWSE 資料源，
    完全沒察覺少了一整層。所以把它也納入檢查。
    """
    import revenue
    ms = sorted(p.name[:6] for p in revenue.DIR.glob("*.html.gz"))
    return len(ms), (ms[-1] if ms else None)


def refresh_revenue():
    """補齊缺的月份、重抓最近兩個月。抓不到只警告：舊資料還能用，
    而「太舊」由 main 另外檢查。

    舊版完全沒有這一步 —— 營收快取抓過一次就凍住，新月份永遠進不來，
    超過 75 天營收那 25 分會整批靜靜歸零。"""
    import revenue
    try:
        revenue.update()
    except Exception as e:
        log("::warning::月營收更新失敗：{}".format(e))


def revenue_stale(latest, today=None):
    """最新一個月是不是比「應該要有的」還舊。

    每月 10 日是申報截止；過了 12 日（留兩天緩衝）上個月就該有了，
    在那之前至少要有上上個月。"""
    import datetime as dt
    t = today or dt.date.today()
    back = 1 if t.day > 12 else 2
    y, m = t.year, t.month - back
    while m < 1:
        y, m = y - 1, m + 12
    return latest is None or latest < "{:04d}{:02d}".format(y, m)


def refresh_margin(cal, cutoff):
    """融資券是最晚公布的（約 21:30），下午的更新通常還拿不到。

    舊版的問題：某天的三張核心表齊了之後，那一天就不會再被抓，融資券從此缺著，
    而且沒有任何錯誤。穩定強勢股名單（第 11 輪）有兩條用到融資：融資暴增排除、籌碼乾淨，
    缺值時會安靜地變成「不排除、給中間值」。所以每次都補抓最近還沒拿到的日子，
    只重寫 margin 表。回傳這次新補進來的交易日。"""
    days = [d for d in cal[-LOOKBACK:] if d <= cutoff and not ingest.have("margin", d)]
    if not days:
        return []
    log("融資券還沒拿到的交易日: {}".format(", ".join(days)))
    ingest.backfill(["margin"], days)
    got = [d for d in days if ingest.have("margin", d)]
    if got:
        store.append(got, verbose=True, datasets=("margin",))
    log("融資券補進 {} 天：{}".format(len(got), "、".join(got) or "（還沒公布）"))
    return got


def main(ignore_gate=False):
    # 網頁按「更新」帶進來的 PTT 聲量：先併進匯總檔，後面不管走哪條路，發佈時都讀得到。
    # 走環境變數、不進命令列：內容來自公開入口，不能讓它有機會被當成指令的一部分。
    payload = os.environ.get("SOCIAL_PAYLOAD", "").strip()
    if payload:
        try:
            import social
            social.apply_payload(payload)
        except Exception as e:
            log("::warning::社群聲量併入失敗，這次沿用原本的匯總：{}".format(e))
    refresh_revenue()
    n_rev, rev_latest = check_revenue()
    log("月營收資料: {} 個月，最新 {}".format(n_rev, rev_latest))
    if n_rev and revenue_stale(rev_latest):
        log("::warning::月營收最新只到 {}，比應該有的舊。"
            "營收超過 75 天會被當成缺值，營收那 25 分會整批歸零。".format(rev_latest))
    if not n_rev and not ignore_gate:
        write_summary([
            "### 沒有發佈",
            "",
            "**找不到月營收資料**（`raw/revenue_mops/`）。",
            "",
            "月營收不在 DuckDB 裡，是 publish 時直接從原始檔讀的。少了它，",
            "漏斗的 L3 營收層會整層失效 —— 候選名單會虛胖將近兩成，",
            "而且不會有任何錯誤訊息。",
            "",
            "公開資訊觀測站抓不到、種子也沒有帶 `raw/revenue_mops/`。在本機跑 `python revenue.py` 再 `python seed.py`。",
        ])
        set_output("publish", "false")
        set_output("changed", "false")
        log("::error::缺少月營收資料，不發佈。")
        return 0

    last_ok = usable_last_date()
    log("倉儲目前最新（三表齊備）: {}".format(_d(last_ok)))
    set_output("data_date", _d(last_ok))      # 網站快取的鍵（見 workflow 的「還原上次產出的網站」）

    cal = ingest.trading_calendar()          # 當月一定重抓，否則日曆會停住
    cutoff = last_ok.strftime("%Y%m%d") if last_ok is not None else "00000000"
    todo = [d for d in cal[-LOOKBACK:] if d > cutoff]

    if not todo:
        # 晚上 21:30 之後的更新：當天的融資券公布了，就用完整資料重新發佈一次。
        # 名單以這一版為準（下午那版的融資券是前一天的）。
        got = refresh_margin(cal, cutoff)
        import social                         # 社群聲量（只顯示）：每次要發佈都更新
        if cutoff in got or not automatic():
            social.update()
        if cutoff in got:
            write_summary(["當天（{}）的融資券已公布，用完整資料重新產出名單。".format(_d(last_ok))])
            set_output("publish", "true")
            set_output("changed", "true")
            return 0
        if backfill_mode():
            # 補歷史：沒有新的交易日也要抓上櫃與 ETF 的歷史，抓完完整產出（上櫃個股頁才會出現）
            extras()
            write_summary(["補歷史：倉儲已是最新（**{}**），這次補了上櫃與主動式 ETF 的歷史資料。"
                           .format(_d(last_ok))])
            set_output("publish", "true")
            set_output("changed", "true")
            return 0
        # 開網頁的自動觸發一天會來很多次；第一次發佈之後，後面都會走到這裡。
        # 自動觸發就直接略過，不要每次都重做一次產出。
        # 手動觸發則照樣重新產出 —— 那通常是改了程式碼、想讓網站跟上。
        scheduled = automatic()
        log("沒有新的交易日，倉儲已是最新。")
        if scheduled:
            write_summary(["倉儲已是最新：**{}**。今天已經發佈過，這次自動觸發略過。"
                           .format(_d(last_ok))])
            set_output("publish", "false")
        else:
            write_summary(["倉儲已是最新：**{}**，沒有新的交易日。".format(_d(last_ok)),
                           "", "手動觸發：資料沒變，程式碼也沒變的話只更新社群聲量（快速通道），"
                           "否則重新產出整個網站。"])
            set_output("publish", "true")
            # 快速通道：行情、法人、融資券都沒有新東西，網站上會變的只有社群聲量。
            # workflow 會去找「同一個資料日期、同一份程式碼」上次產出的網站；找得到就只重算
            # 社群聲量那兩張榜（幾秒），找不到（改過程式、或快取過期）就照舊整個重新產出。
            # 補進了前幾天的融資券也不走快速通道 —— 那會改到名單用的融資特徵。
            set_output("light", "false" if got else "true")
        # 補進了前幾天的融資券（不是當天的）也要存快取，否則下次又要重抓一遍
        set_output("changed", "true" if got else "false")
        return 0

    log("要補的交易日: {}".format(", ".join(todo)))
    ingest.backfill(list(ingest.DATASETS), todo)

    # 閘門：逐日檢查三個核心資料源是不是都拿到了
    ready, missing = [], {}
    for d in todo:
        lack = [ds for ds in CORE if not ingest.have(ds, d)]
        if lack:
            missing[d] = lack
        else:
            ready.append(d)

    if not ready:
        names = {"mi_index": "行情", "bwibbu": "估值", "t86": "三大法人"}
        detail = "；".join(
            "{} 缺 {}".format(d, "、".join(names.get(x, x) for x in lack))
            for d, lack in missing.items())
        log("資料不齊，不發佈。{}".format(detail))
        if not ignore_gate:
            write_summary([
                "### 沒有發佈",
                "",
                "TWSE 還沒把 **{}** 的資料發完，所以這次不更新網站。".format(todo[0]),
                "",
                "```",
                detail,
                "```",
                "",
                "各資料源的發布時間不同（行情約 14:30，估值與三大法人更晚）。",
                "下次有人打開網頁或按 ⟳ 時會再試（5 分鐘內不會重複觸發）。現在發佈的話，",
                "殖利率那層排除規則會因為缺值而靜默失效，名單會虛胖將近一倍。",
            ])
            set_output("publish", "false")
            set_output("changed", "false")
            return 0
        log("--ignore-gate：仍然繼續。")

    if missing and ready:
        log("部分日期資料不齊，只採用: {}".format(", ".join(ready)))

    # 用增量寫入，不是全量重建。CI 只帶了倉儲種子、沒有完整的原始檔封存，
    # 全量重建會用僅有的幾天原始檔把多年歷史整個蓋掉 —— 2026-09-18 就是這樣
    # 把候選名單變成空的（被 publish 的自我檢查擋下，才沒有上線）。
    log("寫入新交易日：{}".format(", ".join(ready)))
    store.append(ready, verbose=True)
    now_ok = usable_last_date()
    log("完成，三表齊備到 {}".format(_d(now_ok)))
    set_output("data_date", _d(now_ok))       # 寫進新的交易日了；同名的輸出以最後一次為準
    refresh_margin(cal, max(ready))           # 前幾天漏掉的融資券順便補

    extras()      # 上櫃與主動式 ETF：平常只抓新的日子，歷史由補歷史模式負責
    # 社群聲量（只顯示）：這裡只查 Threads（有金鑰才查）。PTT 由網頁按「更新」時送進來（見 main 開頭）。不丟例外。
    import social
    social.update()

    lines = ["資料已更新到 **{}**。".format(_d(now_ok)), ""]
    lines.append("補進 {} 個交易日：{}".format(len(ready), "、".join(ready)))
    if missing:
        lines += ["", "以下日期資料尚未發完，這次沒有採用："]
        lines += ["- {}（缺 {}）".format(d, "、".join(v)) for d, v in missing.items()]
    write_summary(lines)
    set_output("publish", "true")
    set_output("changed", "true")
    return 0


if __name__ == "__main__":
    sys.exit(main(ignore_gate="--ignore-gate" in sys.argv))
