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
    """月營收資料在不在。

    這一項不在上面的三張表裡：月營收是 publish 時由 revenue.load() 從
    raw/revenue/ 直接讀的，不進 DuckDB。所以倉儲看起來完全正常，
    但只要那個資料夾沒跟著過來，yoy 就全是缺值，漏斗的 L3 營收層
    <b>整層失效</b> —— 候選名單從 221 檔虛胖成 263 檔，沒有任何錯誤訊息。

    第一次部署上線就是這樣中的：閘門只檢查三個 TWSE 資料源，
    完全沒察覺少了一整層。所以把它也納入檢查。
    """
    from config import RAW
    rev = RAW / "revenue"
    return len(list(rev.glob("*.json.gz"))) if rev.exists() else 0


def main(ignore_gate=False):
    n_rev = check_revenue()
    log("月營收資料: {} 檔".format(n_rev))
    if not n_rev and not ignore_gate:
        write_summary([
            "### 沒有發佈",
            "",
            "**找不到月營收資料**（`raw/revenue/`）。",
            "",
            "月營收不在 DuckDB 裡，是 publish 時直接從原始檔讀的。少了它，",
            "漏斗的 L3 營收層會整層失效 —— 候選名單會虛胖將近兩成，",
            "而且不會有任何錯誤訊息。",
            "",
            "多半是種子檔沒有包含 `raw/revenue/`。在本機重跑 `python seed.py` 即可。",
        ])
        set_output("publish", "false")
        log("::error::缺少月營收資料，不發佈。")
        return 0

    last_ok = usable_last_date()
    log("倉儲目前最新（三表齊備）: {}".format(_d(last_ok)))

    cal = ingest.trading_calendar()          # 當月一定重抓，否則日曆會停住
    cutoff = last_ok.strftime("%Y%m%d") if last_ok is not None else "00000000"
    todo = [d for d in cal[-LOOKBACK:] if d > cutoff]

    if not todo:
        log("沒有新的交易日，倉儲已是最新。")
        write_summary(["倉儲已是最新：**{}**，沒有新的交易日。".format(_d(last_ok)),
                       "", "仍會重新產出網站，確保線上內容與倉儲一致。"])
        set_output("publish", "true")
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
                "晚一點再按一次 Run workflow 就好 —— 現在發佈的話，",
                "殖利率那層排除規則會因為缺值而靜默失效，名單會虛胖將近一倍。",
            ])
            set_output("publish", "false")
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

    lines = ["資料已更新到 **{}**。".format(_d(now_ok)), ""]
    lines.append("補進 {} 個交易日：{}".format(len(ready), "、".join(ready)))
    if missing:
        lines += ["", "以下日期資料尚未發完，這次沒有採用："]
        lines += ["- {}（缺 {}）".format(d, "、".join(v)) for d, v in missing.items()]
    write_summary(lines)
    set_output("publish", "true")
    return 0


if __name__ == "__main__":
    sys.exit(main(ignore_gate="--ignore-gate" in sys.argv))
