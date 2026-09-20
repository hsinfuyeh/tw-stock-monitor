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


def run(check):
    test_barrier(check)
    test_score(check)


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
