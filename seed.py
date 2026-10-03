"""把本機倉儲上傳成 GitHub Release，給 CI 冷啟動用。

為什麼需要種子 ——

    CI 的 runner 每次都是乾淨的。倉儲放 Actions 快取可以撐一陣子，但快取
    閒置 7 天會被清掉（連假就可能中招）。那時候要從零回補，等於
    7,492 次請求 × 2.5 秒 ≈ 5.2 小時，會撞到 job 的時間上限。

    所以把壓實後的倉儲放一份在 Release 上。冷啟動就下載它，之後只補當天。

種子裡有什麼 ——

    data/twse.duckdb   壓實後約 271 MB（2008-2026，4,597 個交易日）。
    raw/revenue_mops/  月營收，公開資訊觀測站月報表，2007 年起每月一頁。
    raw/active_etf/    主動式 ETF 持股快照（有的投信無法回補，這是唯一來源）。

    月營收必須一起帶。它不在 DuckDB 裡（publish 時才由 revenue.load()
    從原始檔讀），CI 沒有它的話 yoy 全是缺值，漏斗的 L3 營收層會整層失效
    —— 候選名單從 221 檔虛胖成 263 檔，而且不會有任何錯誤訊息。
    第一次上線就是這樣中的。

    raw/ 其餘部分（8,071 個檔案共 626 MB）不帶：CI 只負責產出當日網站，
    不需要重跑解析。真要改解析邏輯，在本機重建再種一次即可。

    raw/tpex/          上櫃原始檔（約 40 MB）。tpex.update 用「原始檔在不在」判斷缺哪幾天，
                       不帶的話冷啟動後補歷史會把 300 天全部重抓一遍。

種子會自己更新 ——

    種子太舊也會出事：CI 冷啟動後要從種子的最後一天一路補到今天。所以 workflow 每次存倉儲快取之後
    跑 python seed.py --if-older 7，種子超過 7 天就用 CI 手上的倉儲重新上傳（約 1–2 分鐘），
    一週最多一次。本機跑也可以，效果一樣。

主動式 ETF 持股另外備份 ——

    群益、國泰只能抓當天的持股、無法回補，raw/active_etf/ 是唯一來源。種子一週才更新一次，
    快取被清掉（7 天沒人打開網站）時會掉最多一週的持股快照，而且補不回來。
    所以每次有寫進新資料的更新都跑 python seed.py --etf，把 raw/active_etf/（約 2 MB）
    另外存成同一個 Release 的 active_etf.tar.gz；冷啟動時解完種子再解這一份（比種子新）。
    上傳前先看 Release 上那一份有幾個檔：這次的比較少就不蓋（冷啟動沒還原成功時，
    手上的是種子裡的舊版，蓋上去等於把新的備份換成舊的）。

跑法：
    python seed.py                上傳／更新種子
    python seed.py --if-older 7   種子超過 7 天才更新（CI 用）
    python seed.py --etf          只備份主動式 ETF 持股（CI 每次有新資料時用）
    python seed.py --check        只看現在的狀態，不動任何東西
"""
import datetime as dt
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from config import DB, RAW

TAG = "warehouse-seed"
ASSET = "seed.tar.gz"
ETF_ASSET = "active_etf.tar.gz"
ROOT = Path(__file__).resolve().parent


def sh(*args, **kw):
    return subprocess.run(args, cwd=str(ROOT), text=True,
                          capture_output=True, **kw)


def repo():
    r = sh("gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
    if r.returncode:
        sys.exit("拿不到 repo 名稱，確認 gh 已登入：\n" + r.stderr.strip())
    return r.stdout.strip()


def seed_age_days():
    """種子上傳到現在幾天；還沒有種子回傳 None。"""
    r = sh("gh", "release", "view", TAG, "--json", "assets",
           "-q", '.assets[] | select(.name == "{}") | .updatedAt'.format(ASSET))
    ts = r.stdout.strip()
    if r.returncode or not ts:
        return None
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400


def etf_backup():
    """把 raw/active_etf/ 備份成 Release 上的 active_etf.tar.gz（見檔頭）。失敗只印訊息、回 False。"""
    act = RAW / "active_etf"
    files = [p for p in act.rglob("*") if p.is_file()] if act.exists() else []
    if not files:
        print("沒有 raw/active_etf/，不備份。")
        return False
    with tempfile.TemporaryDirectory() as tmp:
        old = Path(tmp) / "old"
        old.mkdir()
        r = sh("gh", "release", "download", TAG, "--pattern", ETF_ASSET, "--dir", str(old))
        if r.returncode == 0:
            with tarfile.open(old / ETF_ASSET) as t:
                n_old = sum(1 for m in t.getmembers() if m.isfile())
            if len(files) < n_old:
                print("::warning::手上的主動式 ETF 持股（{} 個檔）比 Release 上的備份（{} 個）少，不蓋掉備份。"
                      .format(len(files), n_old))
                return False
        else:
            n_old = 0
        tar = Path(tmp) / ETF_ASSET
        with tarfile.open(tar, "w:gz") as t:
            t.add(str(act), arcname="raw/active_etf")
        r = sh("gh", "release", "upload", TAG, str(tar), "--clobber")
        if r.returncode:
            print("::warning::主動式 ETF 持股備份上傳失敗：" + r.stderr.strip()[:200])
            return False
        print("主動式 ETF 持股備份：{} 個檔（原本 {} 個），{:.1f} MB".format(
            len(files), n_old, tar.stat().st_size / 1e6))
        return True


def main(check_only=False, if_older=None):
    if if_older is not None:
        age = seed_age_days()
        if age is not None and age < if_older:
            print("種子是 {:.1f} 天前的，還不到 {} 天，不更新。".format(age, if_older))
            return
        print("種子{}，更新。".format("還沒建立" if age is None else "是 {:.1f} 天前的".format(age)))
    if not DB.exists():
        sys.exit("找不到倉儲 {} —— 先跑 python run.py daily".format(DB))
    mb = DB.stat().st_size / 1e6
    name = repo()
    print("repo   {}".format(name))
    print("倉儲   {}  {:.0f} MB".format(DB, mb))

    r = sh("gh", "release", "view", TAG, "--json",
           "tagName,assets", "-q", ".assets[].name")
    exists = r.returncode == 0
    print("種子   {}".format(
        "已存在（" + r.stdout.strip().replace("\n", ", ") + "）" if exists else "尚未建立"))
    if check_only:
        return

    rev = RAW / "revenue_mops"
    n_rev = len(list(rev.glob("*.html.gz"))) if rev.exists() else 0
    print("月營收   {} 個月".format(n_rev))
    if not n_rev:
        sys.exit("找不到月營收資料。沒有它，CI 產出的漏斗會少一整層而且"
                 "不會報錯 —— 先跑 python revenue.py")

    if mb > 1900:
        sys.exit("倉儲 {:.0f} MB 超過 Release 單檔 2 GB 上限。".format(mb))

    if not exists:
        print("建立 Release…")
        r = sh("gh", "release", "create", TAG,
               "--title", "倉儲種子",
               "--notes", "CI 冷啟動用的 DuckDB 倉儲快照。由 seed.py 產生，"
                          "內容可從 raw/ 完整重建，不是不可取代的資料。",
               "--latest=false")
        if r.returncode:
            sys.exit("建立失敗：\n" + r.stderr.strip())

    with tempfile.TemporaryDirectory() as tmp:
        tar = Path(tmp) / ASSET
        print("打包…")
        with tarfile.open(tar, "w:gz") as t:
            t.add(str(DB), arcname="data/" + DB.name)
            t.add(str(rev), arcname="raw/revenue_mops")
            # 主動式 ETF 持股快照。群益、國泰只能抓當天、無法回補，
            # 這份原始檔是唯一來源（倉儲裡的表也有一份，這是備援）。
            act = RAW / "active_etf"
            if act.exists():
                t.add(str(act), arcname="raw/active_etf")
            otc = RAW / "tpex"
            if otc.exists():
                t.add(str(otc), arcname="raw/tpex")
        size = tar.stat().st_size / 1e6
        print("上傳 {:.0f} MB（會覆蓋舊的）…".format(size))
        r = sh("gh", "release", "upload", TAG, str(tar), "--clobber")
        if r.returncode:
            sys.exit("上傳失敗：\n" + r.stderr.strip())
    print("完成。CI 冷啟動時會自動抓這一份。")


if __name__ == "__main__":
    a = sys.argv
    if "--etf" in a:
        etf_backup()          # 失敗只印警告，不讓 workflow 變紅燈
        sys.exit(0)
    main(check_only="--check" in a,
         if_older=float(a[a.index("--if-older") + 1]) if "--if-older" in a else None)
