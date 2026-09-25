"""研究的統一入口。RESEARCH_LOG.md 裡的每個數字都能用這支重跑。

    python study.py spec     第 1 輪：事前登記的 23 組選股（M1–M4、baseline、PRD）
    python study.py exit     第 2 輪：7 種停損
    python study.py hold     第 3 輪：目標價上限與持有期
    python study.py bench    第 4 輪：改用 0050 當基準
    python study.py target   第 5 輪：淨賺 5% 的目標價 ＋ 波動門檻
    python study.py all      全部

特徵面板（約 20 秒）算完會快取在 data/study_panel.pkl，之後各輪共用；
倉儲或月營收更新過就會自動重算。加 --fresh 可以強制重算。

為什麼要有這支：原本每一輪都吃我手動產生的暫存檔，換一台電腦就跑不出來，
等於「結論寫在 RESEARCH_LOG，但沒人能重現」。研究要能被檢查才有意義。
"""
import subprocess
import sys
import time

import pandas as pd

import barrier
from config import DATA, DB

CACHE = DATA / "study_panel.pkl"
ROUNDS = {"spec": "study_spec.py", "exit": "study_exit.py", "hold": "study_hold.py",
          "bench": "study_bench.py", "target": "study_target.py"}


def panel(fresh=False):
    """特徵 ＋ 標籤的面板。倉儲沒變就重用快取。"""
    # 月營收不在倉儲裡（raw/revenue_mops/），只看倉儲的時間會讓營收換了面板卻沒重算
    import revenue
    rv = sorted(revenue.DIR.glob("*.html.gz"))
    mtime = (DB.stat().st_mtime, len(rv), max((p.stat().st_mtime for p in rv), default=0))
    if not fresh and CACHE.exists():
        try:
            d = pd.read_pickle(CACHE)
            if d.attrs.get("db_mtime") == mtime:
                print("沿用快取：{}（{:,} 列）".format(CACHE, len(d)), flush=True)
                return d
            print("倉儲已更新，重算特徵…", flush=True)
        except Exception:
            pass
    t0 = time.time()
    print("計算特徵面板（約 20 秒）…", flush=True)
    d = barrier.features()
    d = d.join(barrier.labels(d))
    d.attrs["db_mtime"] = mtime
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    d.to_pickle(CACHE)
    print("完成：{:,} 列（{} ~ {}），{:.0f} 秒".format(
        len(d), str(d["date"].min())[:10], str(d["date"].max())[:10], time.time() - t0),
        flush=True)
    return d


def main(args):
    fresh = "--fresh" in args
    want = [a for a in args if not a.startswith("--")] or ["all"]
    panel(fresh)                       # 先確保快取是新的，各輪直接讀
    names = list(ROUNDS) if want == ["all"] else want
    for n in names:
        if n not in ROUNDS:
            print("不認得的輪次：{}（可用：{}）".format(n, "、".join(ROUNDS)))
            return 2
        print("\n" + "=" * 70 + "\n{}（{}）\n".format(n, ROUNDS[n]) + "=" * 70, flush=True)
        r = subprocess.run([sys.executable, ROUNDS[n], str(CACHE)])
        if r.returncode:
            print("::error::{} 失敗".format(ROUNDS[n]))
            return r.returncode
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
