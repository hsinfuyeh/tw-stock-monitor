"""把本機倉儲上傳成 GitHub Release，給 CI 冷啟動用。

為什麼需要種子 ——

    CI 的 runner 每次都是乾淨的。倉儲放 Actions 快取可以撐一陣子，但快取
    閒置 7 天會被清掉（連假就可能中招）。那時候要從零回補，等於
    7,492 次請求 × 2.5 秒 ≈ 5.2 小時，會撞到 job 的時間上限。

    所以把壓實後的倉儲放一份在 Release 上。冷啟動就下載它，之後只補當天。

為什麼是倉儲而不是原始檔 ——

    raw/ 有 8,071 個檔案共 626 MB，倉儲壓實後只有 148 MB。
    CI 只負責「產出當日網站」，不需要重跑解析；真要改解析邏輯，
    在本機重建再重新種一次即可。

跑法：
    python seed.py            上傳／更新種子
    python seed.py --check    只看現在的狀態，不動任何東西
"""
import subprocess
import sys
from pathlib import Path

from config import DB

TAG = "warehouse-seed"
ROOT = Path(__file__).resolve().parent


def sh(*args, **kw):
    return subprocess.run(args, cwd=str(ROOT), text=True,
                          capture_output=True, **kw)


def repo():
    r = sh("gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
    if r.returncode:
        sys.exit("拿不到 repo 名稱，確認 gh 已登入：\n" + r.stderr.strip())
    return r.stdout.strip()


def main(check_only=False):
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

    print("上傳 {:.0f} MB（會覆蓋舊的）…".format(mb))
    r = sh("gh", "release", "upload", TAG, str(DB), "--clobber")
    if r.returncode:
        sys.exit("上傳失敗：\n" + r.stderr.strip())
    print("完成。CI 冷啟動時會自動抓這一份。")


if __name__ == "__main__":
    main(check_only="--check" in sys.argv)
