"""短線核心的正確性測試（由 tests.py 呼叫，也可以自己跑：python tests_short.py）。

為什麼要單獨測這一塊：清單與參考價位現在都由 shortterm.py 與 barrier.py 決定，
而這個專案反覆出現同一種失敗 —— <b>某條規則安靜地失效，畫面照樣顯示</b>。
2026-09-20 就中過一次：目標價關掉之後，Infinity 被寫進 JSON，
回測頁整頁空白，產出流程完全沒有報錯，是人工點開頁面才發現的。

所以這裡每一條測的都是「錯了也不會報錯」的情境：跳空、同日碰到兩邊、
鎖漲跌停、除權息、持有期走不完、以及寫出去的 JSON 合不合法。
全部用合成資料，不碰資料庫，所以很快。
"""
import json

import numpy as np
import pandas as pd

FLAT = (100.0, 100.0, 100.0, 100.0)


def panel(rows, code="AAA"):
    """造一段合成日線。rows 是 (開, 高, 低, 收) 的序列。"""
    d = pd.DataFrame(rows, columns=["open", "high", "low", "close"], dtype=float)
    d.insert(0, "code", code)
    d.insert(0, "date", pd.date_range("2026-01-01", periods=len(rows), freq="D"))
    d["k"] = 1.0
    d["prev_close"] = d["close"].shift(1)
    return d


def test_barrier(check):
    """10 日 / +5% 先觸及標籤。進場是第 2 列的開盤價（100），目標 105、停損 97。"""
    print("\n短線標籤（barrier.labels）")
    import barrier

    def lab(rows, d=None):
        d = panel(rows) if d is None else d
        return barrier.labels(d, hold=5).iloc[0]

    r = lab([FLAT, FLAT, FLAT, (100, 106, 100, 105), FLAT, FLAT])
    check("目標先到 -> +1、以目標價成交",
          r["label"] == 1 and abs(r["net"] - (5 - 0.585)) < 1e-6,
          "第 {:.0f} 天出場，淨 {:.3f}%".format(r["day"], r["net"]))

    r = lab([FLAT, FLAT, (100, 100, 96, 97), FLAT, FLAT, FLAT])
    check("停損先到 -> −1、以停損價成交",
          r["label"] == -1 and abs(r["net"] - (-3 - 0.585)) < 1e-6)

    # 同一根 K 同時碰到上下界：無法得知先後，必須保守算停損
    r = lab([FLAT, (100, 106, 96, 100), FLAT, FLAT, FLAT, FLAT])
    check("同日碰到兩邊 -> 算停損（保守）", r["label"] == -1)

    # 跳空穿過界線：成交在開盤價，不是假設剛好成交在界線上
    r = lab([FLAT, FLAT, (108, 109, 107, 108), FLAT, FLAT, FLAT])
    check("跳空穿過目標 -> 以開盤價成交",
          r["label"] == 1 and abs(r["net"] - (8 - 0.585)) < 1e-6,
          "淨 {:.3f}%".format(r["net"]))

    r = lab([FLAT, FLAT, (93, 94, 92, 93), FLAT, FLAT, FLAT])
    check("跳空穿過停損 -> 以開盤價成交",
          r["label"] == -1 and abs(r["net"] - (-7 - 0.585)) < 1e-6)

    r = lab([FLAT] * 6)
    check("都沒碰到 -> 到期，以最後一天收盤出場", r["label"] == 0 and r["day"] == 5)

    # 隔天一字漲停：買不到，這筆不能算成交（回測最常見的美化來源）
    r = lab([FLAT, (110, 110, 110, 110), FLAT, FLAT, FLAT, FLAT])
    check("隔天一字漲停 -> 買不到，不給標籤", pd.isna(r["label"]))

    # 持有中整天鎖跌停：當天賣不掉，要順延
    r = lab([FLAT, FLAT, (90, 90, 90, 90), (95, 95, 94, 95), FLAT, FLAT])
    check("持有中鎖跌停 -> 當天不出場，隔天才賣",
          r["label"] == -1 and r["day"] == 3,
          "第 {:.0f} 天出場".format(r["day"]))

    # 除權息：配 5 元造成的價格落差不該被當成跌破停損。
    # 除息日之後價格就停在 95（真實情況），還原後一直是 100 -> 應該是到期，不是停損。
    ex = (95.0, 95.0, 95.0, 95.0)
    d = panel([FLAT, FLAT, ex, ex, ex, ex])
    d.loc[2:, "k"] = 100 / 95.0
    r = barrier.labels(d, hold=5).iloc[0]
    check("除息的跳空不算觸及停損（用還原係數比較）",
          r["label"] == 0 and abs(r["net"] - (-0.585)) < 1e-6,
          "label={} 淨 {:.3f}%".format(r["label"], r["net"]))

    r = lab([FLAT, FLAT, (100, 107, 94, 100), FLAT, FLAT, FLAT])
    check("最高／最低漲跌幅以進場價為基準",
          abs(r["mfe"] - 7) < 1e-6 and abs(r["mae"] - (-6)) < 1e-6)

    # 持有期走不完就不給標籤 —— 不能拿不存在的未來補
    r = barrier.labels(panel([FLAT] * 3), hold=5).iloc[0]
    check("持有期走不完 -> 不給標籤", pd.isna(r["label"]))


def test_score(check):
    """評分、門檻與參考價位。"""
    print("\n短線評分與參考價位（shortterm）")
    import shortterm as st

    p = st.params()
    base = dict(code="A", name="測試", kind="個股", close=100.0, ma5=99.0, ma10=98.0,
                ma20=97.0, hi20_prev=95.0, volume=3e6, vol5_prev=1e6,
                vol20_lots=5000.0, amt20=5e8, streak_f=5.0, streak_t=0.0,
                yoy=30.0, rev_high12=1.0, atrp14=0.04)
    sc = st.score(pd.DataFrame([base]), p)
    check("四個條件全中 -> 100 分", float(sc["score"][0]) == 100.0,
          "{} 分".format(float(sc["score"][0])))

    etf = dict(base, kind="ETF", yoy=np.nan, rev_high12=np.nan)
    check("ETF 沒有月營收 -> 用後三項換算成 100 分",
          float(st.score(pd.DataFrame([etf]), p)["score"][0]) == 100.0)

    check("波動不到門檻 -> 排除",
          not bool(st.score(pd.DataFrame([dict(base, atrp14=0.01)]), p)["liq"][0]))
    check("成交量不到門檻 -> 排除",
          not bool(st.score(pd.DataFrame([dict(base, vol20_lots=100.0)]), p)["liq"][0]))

    px, atrp = np.array([100.0]), np.array([0.04])
    stop, target = st.ref_prices(px, atrp, p)
    check("停損 = 進場 − 3×ATR（夾在上限 10%）", abs(stop[0] - 90.0) < 1e-9, str(stop[0]))
    net = (target[0] / 100.0 - 1) * 100 - st.COST
    check("賣在目標價、扣完成本後真的淨賺設定值",
          net >= p["target_net"] - 1e-9, "實際淨賺 {:.3f}%".format(net))
    check("ATR 太小時停損距離夾在下限 2%",
          abs(st.ref_prices(px, np.array([0.001]), p)[0][0] - 98.0) < 1e-9)
    check("關掉目標價 -> 沒有目標價",
          bool(np.isnan(st.ref_prices(px, atrp, st.params(use_target=0))[1][0])))

    row = dict(base, c1=True, c1hi=True, c2=True, c3=True, c4=True)
    why = st.reasons(row, st.params(w_brk=0))
    check("權重設 0 的條件不會出現在入選理由裡",
          not any("突破" in x for x in why), "；".join(why))

    # 寫出去的 JSON 必須合法：NaN / Infinity 會讓整頁載入失敗
    obj = {"a": np.float64("nan"), "b": float("inf"), "c": np.int64(3),
           "d": [np.float32(1.5), None], "e": np.bool_(True)}
    got = json.loads(json.dumps(st._json_safe(obj), allow_nan=False))
    check("寫檔前把 NaN／Infinity 清成 null（合法 JSON）",
          got["a"] is None and got["b"] is None and got["c"] == 3)


def test_exrights_rows(check):
    """TWT49U 的欄位位置會隨年份變 —— 寫死索引會安靜地解析錯。"""
    print("\n除權息解析（exrights）")
    import exrights

    # 2019 之後：3 前收 4 參考價 5 金額 6 類型
    new = ['108年01月02日', '8473', '山林水', '56.20', '56.09', '0.105651', '權',
           '61.80', '50.50', '56.20', '56.20']
    # 2008–2018：權值與息值分成兩欄，所以金額在 7、類型在 8
    old = ['97年01月02日', '3315', '宣昶', '53.50', '53.15', 0.35, 0.0, '0.350000', '權',
           '57.20', '49.45', '53.50', '53.50']
    a, b = exrights._row(new), exrights._row(old)
    check("新格式（2019 後）解析正確",
          a and a["code"] == "8473" and abs(a["value"] - 0.105651) < 1e-9
          and a["kind"] == "權" and a["date"] == "2019-01-02", str(a))
    check("舊格式（權值息值分兩欄）解析正確",
          b and b["code"] == "3315" and abs(b["value"] - 0.35) < 1e-9
          and b["kind"] == "權" and b["date"] == "2008-01-02", str(b))
    check("認不出來的列回傳 None，不會硬塞錯的值",
          exrights._row(['97年01月02日', '1234', 'x', '1', '1']) is None)


def test_forward(check):
    """前瞻實測（forward.py）：規範 v1 的選股、逐筆追蹤、資金規則。"""
    print("\n前瞻實測（forward）")
    import forward as F
    import shortterm as S

    # --- Arm 2 選股：個股、成交額前 30%、z ≤ −2.5、大盤不是 down、依 z 由低到高取 10 檔
    n = 40
    day = pd.DataFrame({
        "code": ["C%02d" % i for i in range(n)], "kind": ["個股"] * (n - 1) + ["ETF"],
        "adtv20": np.linspace(4e9, 1e8, n), "close": [50.0] * n,
        "ret5_z": [-3.0 - i * 0.01 for i in range(n)], "regime": ["up"] * n})
    day.loc[3, "close"] = 9.0                       # 收盤 < 10 元：排除
    day.loc[5, "ret5_z"] = -2.0                     # 不夠超跌：排除
    got = list(F.pick_m4(day)["code"])
    ok = (len(got) <= 10 and "C03" not in got and "C05" not in got
          and all(int(c[1:]) < n * 0.3 + 1 for c in got))
    zs = list(day.set_index("code").loc[got, "ret5_z"])
    check("Arm 2：只取成交額前 30%、排除低價與不夠超跌、最多 10 檔", ok, str(got))
    check("Arm 2：依超跌程度由深到淺排序", zs == sorted(zs), str(zs))
    check("Arm 2：大盤是空頭（down）時不選", not len(F.pick_m4(day.assign(regime="down"))))

    # --- 逐筆追蹤
    p = S.params()
    row = {"code": "AAA", "name": "x", "close": 100.0, "atrp14": 0.02, "stop": 94.0, "target": 105.6}

    def x_of(rows):
        d = panel(rows)
        return d.iloc[1:].reset_index(drop=True)

    t = F.track_trade(x_of([FLAT, (110, 110, 110, 110)] + [FLAT] * 3), row, p)
    check("隔天一字漲停 -> 買不到，不算進場", t["status"] == "買不到（一字漲停）" and t["entry"] is None)
    t = F.track_trade(x_of([FLAT, (104, 105, 103.5, 104)] + [(104, 104.5, 103.5, 104)] * 25), row, p)
    st, tg = S.ref_prices(np.array([104.0]), np.array([0.02]), p)
    check("停損、目標用實際成交價（隔天開盤）重算",
          t["stop_used"] == S._r(st[0]) and t["target_used"] == S._r(tg[0]),
          "{} {} vs {} {}".format(t["stop_used"], t["target_used"], st[0], tg[0]))
    check("兩邊都沒碰到 -> 抱滿天數出場", t["status"] == "時間到出場" and t["days"] == p["hold_days"])
    t = F.track_trade(x_of([FLAT, (100, 100, 100, 100), (90, 91, 89, 90)] + [FLAT] * 25), row, p)
    check("跳空跌破停損 -> 以開盤價出場", t["status"] == "停損出場" and t["exit"] == 90.0, str(t["exit"]))
    g = np.prod([1 + v for _, v in t["path"]])
    check("逐日路徑連乘 = 出場價 / 進場價", abs(g - 0.9) < 1e-9, str(g))

    # --- 資金規則：每天 3 檔候選、同一個產業、第 1 檔被標處置股
    days = ["2026-10-%02d" % i for i in range(1, 16)]

    def cand(sd, i, ind, flags=()):
        ed = days[days.index(sd) + 1]
        xd = days[min(days.index(sd) + 8, len(days) - 1)]
        span = days[days.index(ed):days.index(xd) + 1]
        return {"code": "S%s%d" % (sd[-2:], i), "name": "x", "ind": ind, "flags": list(flags),
                "status": "時間到出場", "entry": 100.0, "stop_used": 95.0, "ret": 0.0,
                "entry_date": ed, "exit_date": xd, "path": [(d, 0.0) for d in span]}
    snaps = [(sd, [cand(sd, 0, "半導體", ["處置股"]), cand(sd, 1, "半導體"), cand(sd, 2, "半導體")])
             for sd in days[:10]]
    bench = pd.Series(1.0, index=days)
    eq, b, trades, expo = F.portfolio(days, snaps, bench, days[0])
    taken = [x for x in trades if x["taken"]]
    per_day = {}
    for x in taken:
        per_day[x["entry_date"]] = per_day.get(x["entry_date"], 0) + 1
    peak = max(sum(1 for x in taken if x["entry_date"] <= d and (x["exit_date"] or "9") > d)
               for d in days)
    check("標成處置股的不買", not any(x["code"].endswith("0") for x in taken))
    check("一天最多新買 1 檔", max(per_day.values()) == 1, str(per_day))
    check("同產業最多同時 2 檔（也就不會超過 3 檔上限）", peak <= 2, str(peak))
    check("每筆部位 = 本金 × 0.5% ÷ 停損距離（5% -> 10%）",
          all(abs(x["weight"] - 10.0) < 0.2 for x in taken), str([x["weight"] for x in taken][:3]))
    check("持平的交易出場後扣掉來回成本",
          all(abs(x["pnl_cap"] + 0.1 * S.COST) < 0.01 for x in taken if x["exit_date"]))
    check("跳過的都有寫原因", all(x.get("why") for x in trades if not x["taken"]))
    snaps = [(sd, [cand(sd, i, "產業%d%s" % (i, sd)) for i in range(3)]) for sd in days[:10]]
    _, _, tr2, _ = F.portfolio(days, snaps, bench, days[0])
    tk2 = [x for x in tr2 if x["taken"]]
    peak = max(sum(1 for x in tk2 if x["entry_date"] <= d and (x["exit_date"] or "9") > d) for d in days)
    check("不同產業時，最多同時持有 3 檔", peak == 3, str(peak))

    m = F.summarize(eq, b, trades, "arm0", expo)
    check("天數不到 120 -> 累積中", m["status"]["label"] == "累積中")
    fake = {"days": 130, "excess": -0.5, "t": -2.5}
    check("第 120 天：落後 ≥ 0.3% 且 t ≤ −2 -> 停用", F.status(fake, "arm1")["label"] == "停用")
    fake = {"days": 250, "excess": 1.0, "t": 3.0, "mdd": -10.0, "hit": 50.0}
    check("第 250 天：Arm 0 達標率不到 55% -> 未通過", F.status(fake, "arm0")["label"] == "未通過")
    check("第 250 天：其他臂不看達標率 -> 成功", F.status(fake, "arm3")["label"] == "成功")


def test_risk_lists(check):
    print("\n處置股／注意股名單（risk_lists）")
    import risk_lists as R
    j = {"fields": ["編號", "公布日期", "證券代號", "證券名稱", "處置起迄時間"],
         "data": [["1", "115/09/10", "1111", "a", "115/09/11～115/09/24"],
                  ["2", "115/09/01", "2222", "b", "115/09/02～115/09/15"],
                  ["3", "115/09/20", "3333", "c", "壞掉的欄位"]]}
    check("處置期間涵蓋當天的才算，壞掉的列跳過", R.parse_punish(j, "2026-09-22") == ["1111"])
    check("欄位名稱對不上 -> 空名單（不猜位置）", R.parse_punish({"fields": ["x"], "data": [[1]]}, "2026-09-22") == [])
    j = {"fields": ["編號", "證券代號", "證券名稱"], "data": [["1", "3094 ", "a"], ["2", "3094", "a"], ["3", "6168", "b"]]}
    check("注意股去重、去空白", R.parse_notice(j) == ["3094", "6168"])
    check("民國日期轉換", R._roc("115/09/22") == "2026-09-22" and R._roc("115.9.2") == "2026-09-02")


def run(check):
    test_barrier(check)
    test_score(check)
    test_exrights_rows(check)
    test_forward(check)
    test_risk_lists(check)


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
