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


def days_to_fetch(cal, cutoff):
    """倉儲最後一天（cutoff，YYYYMMDD）之後的每一個交易日。

    舊版只看日曆最後 LOOKBACK（20）天：平常沒差，但快取 7 天沒用被清掉、從種子冷啟動時，
    種子如果停在 20 個交易日以前，中間那幾天就永遠不會補，而且沒有任何錯誤。"""
    return [d for d in cal if d > cutoff]


def ready_prefix(todo, have):
    """todo（依日期排好）裡從最早開始、連續三張核心表都齊的那幾天。回傳 (ready, missing)。

    中間有一天沒齊，就停在那天之前：後面齊了的日子也先不寫（missing 裡記成空的原因）。
    舊版把齊了的全寫進去，usable_last_date 就跳過缺的那天往後走，days_to_fetch 只看
    它之後的日子 —— 那天的資料（例如證交所忙的時候 T86 回錯誤）就永遠不會再補，而且沒有任何錯誤。"""
    ready, missing = [], {}
    for d in todo:
        lack = [ds for ds in CORE if not have(ds, d)]
        if lack:
            missing[d] = lack
        elif missing:
            missing[d] = []                   # 齊了，但前面還有一天沒齊，等它齊了一起寫
        else:
            ready.append(d)
    return ready, missing


HOLE_DAYS = 60         # 檢查倉儲最近幾個交易日有沒有洞
CORE_TABLES = ("quotes", "inst", "valuation")


def holes_in(want, dates_by_table):
    """want 裡哪幾天，至少有一張核心表沒有資料。"""
    return [d for d in want if any(d not in dates_by_table.get(t, ()) for t in CORE_TABLES)]


def find_holes(cal, cutoff):
    """倉儲最近 HOLE_DAYS 個交易日（到 cutoff 為止）的洞。ready_prefix 擋住了新的洞，
    這裡抓的是改版前留下來的、或從種子帶進來的。"""
    want = [d for d in cal[-HOLE_DAYS:] if d <= cutoff]
    if not want:
        return []
    start = "{}-{}-{}".format(want[0][:4], want[0][4:6], want[0][6:])
    tbl = {}
    for t in CORE_TABLES:
        r = store.q("SELECT DISTINCT strftime(date, '%Y%m%d') AS d FROM {} WHERE date >= DATE '{}'"
                    .format(t, start))
        tbl[t] = set(r["d"])
    return holes_in(want, tbl)


def heal_holes(cal, cutoff):
    """把倉儲裡的洞重抓一次。回傳 (補好的日子, 還補不起來的日子)。補不起來只警告、不擋發佈：
    那是已經過去的日子，擋住只會讓網站連新的資料都停住。"""
    holes = find_holes(cal, cutoff)
    if not holes:
        return [], []
    log("::warning::倉儲最近 {} 個交易日有洞：{}，重抓一次".format(HOLE_DAYS, "、".join(holes)))
    ingest.backfill(list(ingest.DATASETS), holes)
    fixed = [d for d in holes if all(ingest.have(ds, d) for ds in CORE)]
    if fixed:
        store.append(fixed, verbose=True)
    left = [d for d in holes if d not in fixed]
    log("補好 {}；還缺 {}".format("、".join(fixed) or "（無）", "、".join(left) or "（無）"))
    if left:
        log("::warning::這幾天重抓還是不齊，名單與回測少了這幾天：{}".format("、".join(left)))
    return fixed, left


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


def revenue_fingerprint():
    """月營收原始檔的指紋：有哪些月份、最近三個月的內容。比的是解壓後的內容 ——
    重抓的檔案就算內容一樣，gzip 檔頭的時間也會變。"""
    import gzip
    import hashlib
    import revenue
    files = sorted(revenue.DIR.glob("*.html.gz"))
    h = hashlib.sha1()
    for f in files[-3:]:
        try:
            h.update(gzip.decompress(f.read_bytes()))
        except Exception:
            h.update(f.read_bytes())
    return [f.name for f in files], h.hexdigest()


def refresh_revenue():
    """補齊缺的月份、重抓最近兩個月。抓不到只警告：舊資料還能用，
    而「太舊」由 main 另外檢查。回傳月營收是不是真的變了（有新的月份或內容更正）。

    舊版完全沒有這一步 —— 營收快取抓過一次就凍住，新月份永遠進不來，
    超過 75 天營收那 25 分會整批靜靜歸零。
    「變了沒」是給快速通道看的：營收變了卻走快速通道，網站會停在舊的營收（週末公布的月份
    要等到下一個交易日才上去），新抓的檔案也不會存進快取。"""
    import revenue
    before = revenue_fingerprint()
    try:
        revenue.update()
    except Exception as e:
        log("::warning::月營收更新失敗：{}".format(e))
    changed = revenue_fingerprint() != before
    if changed:
        log("月營收有新的月份或更正，這次要完整產出")
    return changed


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
    rev_changed = refresh_revenue()
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
    # 倉儲讀不到（理論上不會：沒有倉儲時 workflow 會先下載種子）就只看最近幾天，不要從 2008 抓起
    todo = days_to_fetch(cal, cutoff) if last_ok is not None else cal[-LOOKBACK:]

    # 倉儲最近幾十天的洞（改版前留下的、或種子帶進來的）先補
    healed, holes_left = heal_holes(cal, cutoff) if last_ok is not None else ([], [])
    changed_any = rev_changed or bool(healed)  # 不是新交易日，但資料變了：要完整產出、要存快取

    if not todo:
        return no_new_day(cal, cutoff, last_ok, changed_any, holes_left)

    log("要補的交易日: {}".format(", ".join(todo)))
    if len(todo) > LOOKBACK:
        log("::warning::要補 {} 個交易日（倉儲停在 {}），應該是從舊的種子冷啟動；"
            "種子由 seed.py --if-older 每週更新。".format(len(todo), _d(last_ok)))
    ingest.backfill(list(ingest.DATASETS), todo)

    # 閘門：逐日檢查三個核心資料源是不是都拿到了；中間有一天沒齊就停在那天之前
    ready, missing = ready_prefix(todo, ingest.have)
    names = {"mi_index": "行情", "bwibbu": "估值", "t86": "三大法人"}

    def why(lack):
        return "缺 " + "、".join(names.get(x, x) for x in lack) if lack else "齊了，但前面的日子還沒齊"

    if not ready and not ignore_gate:
        detail = "；".join("{} {}".format(d, why(lack)) for d, lack in missing.items())
        log("資料不齊，新的交易日這次不寫。{}".format(detail))
        return no_new_day(cal, cutoff, last_ok, changed_any, holes_left, pending=(todo[0], detail))
    if not ready:
        log("--ignore-gate：仍然繼續。")
        ready = [d for d in todo if d not in missing or not missing[d]]

    if missing:
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
        lines += ["", "以下日期這次沒有寫入，下次會再試："]
        lines += ["- {}（{}）".format(d, why(v)) for d, v in missing.items()]
    lines += hole_lines(holes_left)
    write_summary(lines)
    set_output("publish", "true")
    set_output("changed", "true")
    return 0


def hole_lines(left):
    if not left:
        return []
    return ["", "**倉儲有洞**：{} 重抓還是不齊，名單與回測少了這幾天。".format("、".join(left))]


def no_new_day(cal, cutoff, last_ok, changed_any, holes_left, pending=None):
    """沒有新的交易日可以寫：倉儲已是最新，或新的那天證交所還沒發完（pending = (日期, 明細)）。

    證交所還沒發完的時候按 ⟳：舊版直接「不發佈」，瀏覽器剛讀好的 PTT 聲量跟著丟掉，網頁卻顯示更新完成。
    現在跟「沒有新交易日」走同一條路：手動的照樣發佈（快速通道，只換社群聲量），自動的略過。"""
    lines = []
    if pending:
        lines += ["證交所還沒把 **{}** 的資料發完，這個交易日這次先不寫（名單維持 {}）。"
                  .format(pending[0], _d(last_ok)), "", "```", pending[1], "```", "",
                  "各資料源的發布時間不同（行情約 14:30，估值與三大法人更晚）。下次有人打開網頁或按 ⟳ 時會再試。"
                  "現在寫進去的話，殖利率那層排除規則會因為缺值而靜默失效，名單會虛胖將近一倍。", ""]
    # 晚上 21:30 之後的更新：當天的融資券公布了，就用完整資料重新發佈一次。
    # 名單以這一版為準（下午那版的融資券是前一天的）。
    got = refresh_margin(cal, cutoff)
    import social                             # 社群聲量（只顯示）：每次要發佈都更新
    if backfill_mode():
        # 補歷史：沒有新的交易日也要抓上櫃與 ETF 的歷史，抓完完整產出（上櫃個股頁才會出現）。
        # 放在融資券那條之前：晚上當天融資券剛好進來時，舊版直接發佈、補歷史整個被跳過。
        extras()
        social.update()
        write_summary(lines + ["補歷史：倉儲最新是 **{}**，這次補了上櫃與主動式 ETF 的歷史資料。"
                               .format(_d(last_ok))] + hole_lines(holes_left))
        set_output("publish", "true")
        set_output("changed", "true")
        return 0
    if cutoff in got or changed_any or not automatic():
        social.update()
    if cutoff in got:
        write_summary(lines + ["當天（{}）的融資券已公布，用完整資料重新產出名單。".format(_d(last_ok))]
                      + hole_lines(holes_left))
        set_output("publish", "true")
        set_output("changed", "true")
        return 0
    if changed_any:
        # 不是新的交易日，但資料變了（月營收、補好了倉儲的洞）：不論誰觸發都完整產出一次
        write_summary(lines + ["倉儲最新是 **{}**；月營收或之前缺的日子有更新，重新產出網站。"
                               .format(_d(last_ok))] + hole_lines(holes_left))
        set_output("publish", "true")
        set_output("changed", "true")
        return 0
    # 開網頁的自動觸發一天會來很多次；第一次發佈之後，後面都會走到這裡。
    # 自動觸發就直接略過，不要每次都重做一次產出。
    # 手動觸發則照樣發佈 —— 帶著新的社群聲量，或改了程式碼想讓網站跟上。
    if automatic():
        log("沒有新的交易日可以寫，自動觸發略過。")
        write_summary(lines + ["倉儲最新是 **{}**，這次自動觸發略過。".format(_d(last_ok))]
                      + hole_lines(holes_left))
        set_output("publish", "false")
    else:
        log("沒有新的交易日可以寫，手動觸發：只更新社群聲量（程式碼沒變的話走快速通道）。")
        write_summary(lines + ["倉儲最新是 **{}**，沒有新的交易日可以寫。".format(_d(last_ok)),
                               "", "手動觸發：資料沒變，程式碼也沒變的話只更新社群聲量（快速通道），"
                               "否則重新產出整個網站。"] + hole_lines(holes_left))
        set_output("publish", "true")
        # 快速通道：行情、法人、融資券、月營收都沒有新東西，網站上會變的只有社群聲量。
        # workflow 會去找「同一個資料日期、同一份程式碼」上次產出的網站；找得到就只重算
        # 社群聲量那兩張榜（幾秒），找不到（改過程式、或快取過期）就照舊整個重新產出。
        # 補進了前幾天的融資券也不走快速通道 —— 那會改到名單用的融資特徵。
        set_output("light", "false" if got else "true")
    # 補進了前幾天的融資券（不是當天的）也要存快取，否則下次又要重抓一遍
    set_output("changed", "true" if got else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main(ignore_gate="--ignore-gate" in sys.argv))
