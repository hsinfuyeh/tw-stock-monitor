"""本機排程：抓 PTT 股票板 → 匯總 → 推上 GitHub（social.py 的 PTT 來源）。

為什麼在本機跑：PTT 擋 GitHub 的雲端主機（HTTP 403），家用網路抓得到。
由 Windows 工作排程器「tw-stock-monitor 社群聲量」每天 14:25、19:00、21:35 執行 ——
排在雲端發佈（14:40 起、19:10、21:50）之前，發佈時就讀得到最新的數字。

推送方式刻意用 GitHub API 直接更新 snapshots/social/ptt.json 這一個檔，
<b>不碰本機的 git 工作目錄</b>：你本機正在改、還沒推的東西不會被排程順手推上去，
也不會因為本機有未提交的修改而推送失敗。本機的 repo 下次 git pull 就會拿到。

數字沒變（例如半夜沒人發文）就不推，免得 repo 多一堆沒意義的 commit。
需要這台電腦裝好 gh 並登入過（gh auth status）。結果寫在 data/social_push.log。

手動跑：python social_push.py
"""
import base64
import datetime as dt
import json
import subprocess
import sys
import tempfile
import time

import social
from config import ROOT

REPO = "hsinfuyeh/tw-stock-monitor"
PATH = "snapshots/social/ptt.json"
LOG = ROOT / "data" / "social_push.log"


def log(msg):
    line = "{:%Y-%m-%d %H:%M:%S}  {}".format(dt.datetime.now(), msg)
    if sys.stdout:                   # 工作排程器用 pythonw 跑，沒有主控台
        print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8",
                          cwd=str(ROOT), timeout=120,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))   # 不要閃黑窗


def _body(o):
    """比較用：去掉 updated_at，只看數字有沒有變。"""
    return {k: v for k, v in (o or {}).items() if k != "updated_at"}


def remote():
    """GitHub 上目前那一份：(內容 dict 或 None, sha 或 None)。"""
    r = gh("api", "repos/{}/contents/{}?ref=main".format(REPO, PATH))
    if r.returncode != 0:
        if "Not Found" in (r.stderr + r.stdout):
            return None, None
        raise RuntimeError("讀不到 GitHub 上的匯總檔：" + (r.stderr or r.stdout).strip()[:200])
    j = json.loads(r.stdout)
    try:
        return json.loads(base64.b64decode(j["content"]).decode("utf-8")), j["sha"]
    except (KeyError, ValueError):
        return None, j.get("sha")


def push(obj, sha):
    txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    body = dict(message="社群聲量（PTT）匯總 {}".format(obj["updated_at"][:16].replace("T", " ")),
                content=base64.b64encode(txt.encode("utf-8")).decode("ascii"), branch="main")
    if sha:
        body["sha"] = sha
    # 內容約 100 KB，塞在命令列參數會超過 Windows 的長度上限，所以走檔案
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(body, f)
        fn = f.name
    r = gh("api", "-X", "PUT", "repos/{}/contents/{}".format(REPO, PATH), "--input", fn)
    if r.returncode != 0:
        raise RuntimeError("推送失敗：" + (r.stderr or r.stdout).strip()[:200])


def main():
    t0 = time.time()
    social.prune_ptt()
    eps = [int(f.name.split(".")[1]) for f in social.PTT_DIR.glob("*.json.gz")] \
        if social.PTT_DIR.exists() else []
    # 最舊的一篇不到 30 天前（電腦很久沒開、或第一次跑）：往回補 35 天，「暴增」才有基準
    full = bool(eps) and min(eps) < time.time() - 30 * 86400
    n = social.update_ptt(days=3 if full else 35, budget=5000)
    if n == 0 and social.LAST_ERR[0]:
        log("PTT 抓取可能有問題：{}".format(social.LAST_ERR[0]))
    obj = social.export_ptt()
    old, sha = remote()
    if _body(old) == _body(obj):
        log("PTT 抓了 {} 篇，數字沒變，不推（{:.0f} 秒）".format(n, time.time() - t0))
        return 0
    push(obj, sha)
    days = sorted(obj["days"])
    log("PTT 抓了 {} 篇，已推上 GitHub：{} ~ {}、{} 天（{:.0f} 秒）".format(
        n, days[0] if days else "-", days[-1] if days else "-", len(days), time.time() - t0))
    return 0


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except Exception as e:           # 網路斷了之類：記下來，下一次排程再試
        log("失敗：{}".format(e))
        sys.exit(1)
