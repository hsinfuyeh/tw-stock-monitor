"""穩定強勢股名單（stable.py）的正確性測試（由 tests.py 呼叫，也可以自己跑：python tests_stable.py）。

跟 tests_short 同一個原則：測「錯了也不會報錯」的情境 —— 停牌、下市、
窗口還沒走完、隔天一字漲停、除權息、年代不同的漲跌幅、同產業上限。
全部用合成資料，不碰資料庫。
"""
import numpy as np
import pandas as pd


def panel(closes, code="AAA", start="2026-01-01", opens=None, k=None, locked=None, dates=None):
    n = len(closes)
    d = pd.DataFrame({"code": code,
                      "date": dates if dates is not None else pd.bdate_range(start, periods=n),
                      "close": np.asarray(closes, float)})
    d["open"] = np.asarray(opens if opens is not None else closes, float)
    d["k"] = 1.0 if k is None else np.asarray(k, float)
    d["open_locked_up"] = False if locked is None else np.asarray(locked, bool)
    return d


def test_labels(check):
    print("\n穩定名單的標籤（stable.labels）")
    import stable
    cfg = dict(stable.CFG, hold=5)
    lab = lambda d: stable.labels(d, cfg).iloc[0]

    # 進場 = 第 2 列開盤 100；+5% = 105、−5% = 95
    r = lab(panel([100, 100, 101, 105, 90, 100, 100]))
    check("收盤先到 +5% -> 中（之後跌破不影響）", r["label"] == 1 and r["day"] == 3)
    r = lab(panel([100, 100, 99, 95, 110, 100, 100]))
    check("收盤先到 −5% -> 倒", r["label"] == -1 and r["day"] == 3)
    r = lab(panel([100, 100, 104.9, 95.1, 103, 102, 100]))
    check("都沒碰到 -> 平，r10 為第 5 天報酬（102）", r["label"] == 0 and abs(r["r10"] - 2.0) < 1e-9,
          "{}".format(r.to_dict()))
    # 盤中碰到不算：只有收盤（這裡只給收盤，確認不會拿別的欄位）
    r = lab(panel([100, 100, 104.99, 100, 100, 100, 100]))
    check("差一點點（104.99）不算中", r["label"] == 0)

    # 除權息：第 4 天除息 10 元，k 從 1 變 1.1；還原後是 +5.6%，應該算中
    r = lab(panel([100, 100, 100, 96, 96, 96, 96], k=[1, 1, 1, 1.1, 1.1, 1.1, 1.1]))
    check("除息的價格落差用還原價算，不會被當成下跌", r["label"] == 1, "{}".format(r.to_dict()))

    # 隔天一字漲停：買不到，不給標籤
    r = lab(panel([100, 110, 110, 110, 110, 110, 110], locked=[0, 1, 0, 0, 0, 0, 0]))
    check("隔天開盤一字漲停 -> 不計（NaN）", np.isnan(r["label"]) and not r["entered"])

    # 停牌：這檔少了一個市場交易日（另一檔 BBB 有交易，所以那天在日曆上）
    days = pd.bdate_range("2026-01-01", periods=7)
    a = panel([100, 100, 101, 102, 103, 103], dates=days.delete(3))
    b = panel([50] * 7, code="BBB", dates=days)
    d = pd.concat([a, b], ignore_index=True).sort_values(["code", "date"]).reset_index(drop=True)
    r = stable.labels(d, cfg).iloc[0]
    check("持有中停牌（缺了一個交易日）且還沒分出結果 -> 倒", r["label"] == -1, "{}".format(r.to_dict()))

    # 下市：資料在窗口中途結束，但市場還有交易
    a = panel([100, 100, 101], dates=days[:3])
    d = pd.concat([a, b], ignore_index=True).sort_values(["code", "date"]).reset_index(drop=True)
    r = stable.labels(d, cfg).iloc[0]
    check("持有中下市 -> 倒", r["label"] == -1)

    # 窗口還沒走完（資料到底了）：不給標籤，但有目前報酬
    r = lab(panel([100, 100, 102, 103]))
    check("窗口沒走完、還沒分出結果 -> NaN，rnow = 最新報酬、nd = 已走天數",
          np.isnan(r["label"]) and abs(r["rnow"] - 3) < 1e-9 and r["nd"] == 3, "{}".format(r.to_dict()))
    r = lab(panel([100, 100, 106]))
    check("窗口沒走完但已經 +5% -> 已經是中", r["label"] == 1)
    r = stable.labels(panel([100, 100, 102]), cfg).iloc[-1]
    check("最後一天的訊號：等開盤（沒有進場、沒有標籤）", not r["entered"] and np.isnan(r["label"]))


def test_features(check):
    print("\n穩定名單的特徵")
    import stable
    code = np.array(["A"] * 80)
    t = np.arange(80.0)
    line = stable._smoothness(0.01 * t, code, 60)
    check("完全直線：平滑度 = 斜率 × 1", abs(line[-1] - 0.01) < 1e-9)
    rng = np.random.default_rng(0)
    noisy = stable._smoothness(0.01 * t + rng.normal(0, 0.2, 80), code, 60)
    check("同樣斜率但震盪大 -> 平滑度低很多", noisy[-1] < line[-1] * 0.6, "{:.4f}".format(noisy[-1]))
    check("不滿 60 天 -> NaN", np.isnan(line[58]) and np.isfinite(line[59]))
    two = np.array(["A"] * 40 + ["B"] * 40)
    s = stable._smoothness(0.01 * t, two, 30)
    check("視窗不會跨到另一檔股票", np.isnan(s[40 + 28]) and np.isfinite(s[40 + 29]))

    old, new = pd.Timestamp("2015-05-29"), pd.Timestamp("2015-06-01")
    up = stable.limit_up_price([100.0, 100.0], np.array([old, new], dtype="datetime64[ns]"))
    check("2015-06-01 前漲停 7%、之後 10%", list(up) == [107.0, 110.0], "{}".format(up))
    up = stable.limit_up_price([57.3], np.array([new], dtype="datetime64[ns]"))
    check("漲停價依檔位無條件捨去（63.03 -> 63.0）", abs(up[0] - 63.0) < 1e-9, "{}".format(up))


def test_pick(check):
    print("\n穩定名單的選股")
    import stable
    n = 15
    day = pd.DataFrame({"date": pd.Timestamp("2026-01-05"), "code": ["%04d" % i for i in range(n)],
                        "ind": ["半導體"] * 6 + ["航運"] * 4 + [None] * 5,
                        "adtv20": np.arange(n, dtype=float)})
    sc = pd.Series(np.arange(n, 0, -1, dtype=float), index=day.index)
    pk = stable.pick(day, sc, dict(stable.CFG, top_n=10, ind_max=3))
    inds = day.loc[pk.index, "ind"]
    check("同產業最多 3 檔", (inds == "半導體").sum() == 3 and (inds == "航運").sum() == 3)
    check("沒有產業分類的不互相限制", inds.isna().sum() == 4 and len(pk) == 10)
    check("名次連續 1..N", list(pk["rank"]) == list(range(1, 11)))
    pk = stable.pick(day.head(4), sc.head(4), stable.CFG)
    check("池子不到 10 檔就少給，不補位", len(pk) == 3)

    flags = stable._risk_codes({"disposal": ["1111"], "attention30": ["2222", "1111"],
                                "full_delivery": []})
    check("處置／注意標記合併", flags == {"1111": ["處置股", "近 30 日注意股"], "2222": ["近 30 日注意股"]})
    lists = {"2026-01-01": ["A", "B"], "2026-01-02": ["A", "C"], "2026-01-05": ["A", "C"]}
    st, dropped, prev = stable.streaks(lists, ["A", "C", "D"], "2026-01-06")
    check("連續上榜天數（含今天）", st == {"A": 4, "C": 3, "D": 1}, "{}".format(st))
    check("昨天在榜、今天掉出", dropped == [] and prev == "2026-01-05")
    st, dropped, _ = stable.streaks(lists, ["C"], "2026-01-06")
    check("掉出的代號", dropped == ["A"])


def test_display(check):
    print("\n穩定名單的畫面資料")
    import stable
    r = {"code": "1111", "atrp14": 0.021, "n_limit20": 0.0, "n_gap20": 1.0, "n_spike20": 0.0,
         "ret60": 0.12, "margin5": np.nan}
    ok = {"attention30_status": "ok", "disposal_status": "ok"}
    c = {x["name"]: x for x in stable.checks(r, {}, ok)}
    check("燈號帶實際數字", c["波動"]["val"] == "2.1%" and c["跳空"]["val"] == "1 次")
    check("融資沒資料寫「無資料」，不是 nan", c["融資"]["val"] == "無資料")
    check("名單都抓到、沒被點名 -> 通過", c["點名"]["ok"] is True)
    c = {x["name"]: x for x in stable.checks(r, {}, {"attention30_status": "unavailable", "disposal_status": "ok"})}
    check("注意股名單抓不到 -> 標成不確定（None），不假裝通過", c["點名"]["ok"] is None)
    c = {x["name"]: x for x in stable.checks(r, {"1111": ["處置股"]}, ok)}
    check("被點名 -> 不通過", c["點名"]["ok"] is False)

    d = panel([100, 110, 121], k=[1, 1, 1.1])
    sp = stable.spark(d, "AAA", d["date"].max())
    check("走勢線是相對今天（還原價）的百分比，最後一點 = 100", sp[-1] == 100.0 and abs(sp[0] - 100 / 133.1 * 100) < 0.1,
          "{}".format(sp))
    check("走勢線不含未來的日子", len(stable.spark(d, "AAA", d["date"].iloc[1])) == 2)


def test_perf(check):
    print("\n個股頁：近期表現 vs 大盤")
    import publish
    cal = pd.bdate_range("2026-01-01", periods=30)
    bench = pd.Series(np.arange(100, 130, dtype=float), index=cal)
    # 個股少了中間 10 天（停牌）：「一週」的起點仍要是市場日曆往回 5 個交易日
    keep = list(range(0, 10)) + list(range(20, 30))
    s = pd.DataFrame({"date": cal[keep], "adj": np.arange(100, 130, dtype=float)[keep]})
    r = {x["label"]: x for x in publish.perf_vs_bench(s, bench)}
    check("期間依市場交易日曆往回數，不是數個股自己的列數",
          r["一週"]["stock"] == round((129 / 124 - 1) * 100, 1) and r["一週"]["bench"] == round((129 / 124 - 1) * 100, 1),
          "{}".format(r["一週"]))
    check("起點那天停牌 -> 用之前最近的一筆",
          r["一個月"]["stock"] == round((129 / 109 - 1) * 100, 1) and r["一個月"]["bench"] == round((129 / 109 - 1) * 100, 1),
          "{}".format(r["一個月"]))
    check("上市不到那麼久的期間不列", "一季" not in r)


def test_backfill(check):
    """沒有人更新的交易日要補算快照（網站沒有定時排程了），標 backfilled。"""
    print("\n名單快照補算")
    import json, os, tempfile
    from pathlib import Path
    import stable
    tmp = Path(tempfile.mkdtemp())
    saved = (stable.SNAP, stable.today_list, os.environ.get("FORWARD_PERSIST"), os.environ.get("GITHUB_ACTIONS"))
    stable.SNAP = tmp
    (tmp / "2026-09-22.json").write_text(json.dumps({"date": "2026-09-22", "rows": []}), encoding="utf-8")
    days = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-29", "2026-09-30"]
    d = pd.DataFrame({"date": pd.to_datetime(days)})
    asked = []
    stable.today_list = lambda d_, ds, risk: ([{"rank": 1, "code": "2330", "name": "台積電", "ind": "半導體",
                                               "score": 90.0, "close": 1.0, "why": ["x"]}], {})
    try:
        os.environ.pop("FORWARD_PERSIST", None); os.environ.pop("GITHUB_ACTIONS", None)
        check("不寫快照的地方（本機）不補算", stable.backfill(d, lambda ds: {}) == [])
        os.environ["FORWARD_PERSIST"] = "1"
        got = stable.backfill(d, lambda ds: asked.append(ds) or {"disposal_status": "unknown"})
        check("補的是上一份快照之後、最新一天之前缺的交易日", got == ["2026-09-23", "2026-09-24", "2026-09-29"], str(got))
        check("每一天用自己那天的處置／注意股名單", asked == got)
        snap = json.loads((tmp / "2026-09-24.json").read_text(encoding="utf-8"))
        check("補算的快照標 backfilled，處置股狀態照實記", snap["backfilled"] is True
              and snap["risk_status"]["disposal_status"] == "unknown" and snap["rows"][0]["code"] == "2330")
        check("最新一天不在這裡寫（compute 照常寫）", not (tmp / "2026-09-30.json").exists())
        check("已經有快照的不動、再跑一次沒有東西要補",
              json.loads((tmp / "2026-09-22.json").read_text(encoding="utf-8")).get("backfilled") is None
              and stable.backfill(d, lambda ds: {}) == [])
        many = pd.DataFrame({"date": pd.bdate_range("2026-09-23", periods=20)})
        check("最多補 10 天", len(stable.backfill(many, lambda ds: {}, max_days=10)) == 10)
        # 中間的缺口也要補：9/25、9/26 夾在 9/24 和 9/29 兩份快照中間，舊版只看「最新一份之後」，永遠補不到
        gap = Path(tempfile.mkdtemp())
        stable.SNAP = gap
        for ds in ("2026-09-22", "2026-09-29"):
            (gap / (ds + ".json")).write_text(json.dumps({"date": ds, "rows": []}), encoding="utf-8")
        got = stable.backfill(d, lambda ds: {})
        check("夾在兩份快照中間的缺口也補；第一份快照之前的不補", got == ["2026-09-23", "2026-09-24"], str(got))
    finally:
        stable.SNAP, stable.today_list = saved[0], saved[1]
        for k, v in (("FORWARD_PERSIST", saved[2]), ("GITHUB_ACTIONS", saved[3])):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run(check):
    test_backfill(check)
    test_labels(check)
    test_features(check)
    test_pick(check)
    test_display(check)
    test_perf(check)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    ok = []
    run(lambda name, cond, detail="": (
        ok.append(bool(cond)),
        print("  [{}] {}{}".format("PASS" if cond else "FAIL", name,
                                   ("  -> " + detail) if detail else ""))))
    print("\n通過 {} / {}".format(sum(ok), len(ok)))
    sys.exit(0 if all(ok) else 1)
