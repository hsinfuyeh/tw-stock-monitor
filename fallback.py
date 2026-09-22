"""GitHub 排程的保險：晚上檢查公開網站今天有沒有更新，沒有就從這台電腦觸發一次。

為什麼需要：GitHub Actions 的 cron 不保證會跑。2026-09-21、22 連兩天，
每天排的 5 次（14:40–19:10）只有最後一次有跑，還晚了將近一小時。
那一次也被吃掉的話，當天就沒有前瞻實測的紀錄，只能隔天補算。

由 Windows 工作排程器在交易日晚上執行（見 README「排程保險」）。
只有同時符合這三個條件才會觸發，所以重複執行也不會亂發：
  1. 今天是交易日（TWSE 的交易日曆裡有今天 —— 休市日不觸發）
  2. 公開網站的資料日期不是今天
  3. GitHub 上沒有正在跑或排隊中的更新

需要這台電腦裝好 gh 並登入過（gh auth status）。結果寫在 data/fallback.log。
"""
import datetime as dt
import json
import subprocess
import sys
import urllib.request

from config import ROOT

SITE = "https://hsinfuyeh.github.io/tw-stock-monitor/data/meta.json"
LOG = ROOT / "data" / "fallback.log"


def log(msg):
    line = "{:%Y-%m-%d %H:%M:%S}  {}".format(dt.datetime.now(), msg)
    if sys.stdout:                   # 工作排程器用 pythonw 跑，沒有主控台
        print(line)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8",
                          cwd=str(ROOT), timeout=120,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))   # 不要閃黑窗


def main():
    today = dt.date.today()
    import ingest
    cal = ingest.trading_calendar()
    if today.strftime("%Y%m%d") not in cal:
        log("今天不是交易日（或 TWSE 還沒公布今天的行情），不觸發")
        return 0
    req = urllib.request.Request(SITE + "?t={}".format(int(dt.datetime.now().timestamp())),
                                 headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        meta = json.loads(r.read().decode("utf-8"))
    if meta.get("data_date") == today.isoformat():
        log("網站已經是今天的資料（{}），不用觸發".format(meta.get("built_at")))
        return 0
    runs = gh("run", "list", "--workflow", "update.yml", "-L", "5", "--json", "status")
    if runs.returncode != 0:
        log("gh 讀不到執行紀錄：{}".format(runs.stderr.strip()[:200]))
        return 1
    busy = [x for x in json.loads(runs.stdout) if x["status"] in ("queued", "in_progress", "waiting")]
    if busy:
        log("GitHub 上已經有更新在跑或排隊（{} 個），不重複觸發".format(len(busy)))
        return 0
    r = gh("workflow", "run", "update.yml")
    if r.returncode != 0:
        log("觸發失敗：{}".format(r.stderr.strip()[:200]))
        return 1
    log("網站資料停在 {}，已觸發更新".format(meta.get("data_date")))
    return 0


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except Exception as e:           # 網路斷了之類：記下來，下一次排程再試
        log("檢查失敗：{}".format(e))
        sys.exit(1)
