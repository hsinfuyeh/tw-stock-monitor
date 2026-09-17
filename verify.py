"""統計計算的正確性驗證 —— 用已知答案的合成資料反推。

跑法： python verify.py

為什麼需要這個：
  tests.py 驗的是資料層（欄位解析、除權息、look-ahead 防呆），
  但 IC、十分位回測、Newey-West、自助法這些統計計算本身從未對照過已知答案。
  框架自檢只證明「隨機訊號不會被誤判為顯著」，那是必要條件不是充分條件 ——
  一個把所有 IC 都算成 0 的壞程式也會通過自檢。

做法：構造出真實答案已知的合成面板，檢查程式能不能把答案算回來。
"""
import sys

import numpy as np
import pandas as pd

import factors
import validate

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [{}] {}{}".format("PASS" if cond else "FAIL", name,
                               ("  -> " + detail) if detail else ""))


def synth(n_days=400, n_stocks=200, seed=7, signal=0.0, drift=0.0):
    """合成面板。

    signal: 因子與未來報酬的相關強度（0 = 純雜訊，1 = 完全預測）。
    drift:  每日加在全體身上的共同報酬（用來測市場中性化是否有效）。
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    rows = []
    for d in dates:
        f = rng.standard_normal(n_stocks)
        noise = rng.standard_normal(n_stocks)
        # 未來報酬 = signal*因子 + 雜訊，單位為 %
        fwd = (signal * f + np.sqrt(max(1e-9, 1 - signal ** 2)) * noise) * 3.0 + drift
        for i in range(n_stocks):
            rows.append((d, "S{:03d}".format(i), f[i], fwd[i]))
    return pd.DataFrame(rows, columns=["date", "code", "F", "fwd10"])


# ------------------------------------------------------------------ IC
def test_ic():
    print("\nIC 計算")
    # 完全預測：因子就是未來報酬 -> 每日 Spearman 應為 1.0
    p = synth(signal=0.0)
    p["F"] = p["fwd10"]
    ic = validate.daily_ic(p, "F", horizon=10)
    check("完全預測時 IC = 1.0", abs(ic.mean() - 1.0) < 1e-9,
          "實得 {:.6f}".format(ic.mean()))

    # 完全反向
    p2 = p.copy(); p2["F"] = -p2["fwd10"]
    ic2 = validate.daily_ic(p2, "F", horizon=10)
    check("完全反向時 IC = -1.0", abs(ic2.mean() + 1.0) < 1e-9,
          "實得 {:.6f}".format(ic2.mean()))

    # 純雜訊
    p3 = synth(signal=0.0, seed=11)
    ic3 = validate.daily_ic(p3, "F", horizon=10)
    check("純雜訊時 IC ≈ 0", abs(ic3.mean()) < 0.02, "實得 {:+.4f}".format(ic3.mean()))

    # 已知強度：signal=0.3 時 Pearson 相關應接近 0.3，Spearman 略低
    p4 = synth(signal=0.3, seed=13)
    ic4 = validate.daily_ic(p4, "F", horizon=10)
    check("signal=0.30 時 IC 落在 0.25~0.32", 0.25 < ic4.mean() < 0.32,
          "實得 {:+.4f}".format(ic4.mean()))


# ------------------------------------------------------------------ 市場中性化
def test_neutralize():
    print("\n市場中性化")
    base = synth(signal=0.3, seed=17, drift=0.0)
    shifted = base.copy()
    # 每一天加上不同的共同報酬（模擬大盤漲跌）
    rng = np.random.default_rng(3)
    bump = {d: v for d, v in zip(base["date"].unique(),
                                 rng.standard_normal(base["date"].nunique()) * 5)}
    shifted["fwd10"] = shifted["fwd10"] + shifted["date"].map(bump)

    a = validate.daily_ic(base, "F", horizon=10).mean()
    b = validate.daily_ic(shifted, "F", horizon=10).mean()
    check("加上每日共同報酬後 IC 不變（大盤已被中性化掉）",
          abs(a - b) < 1e-9, "{:+.6f} vs {:+.6f}".format(a, b))

    r1 = validate.decile_backtest(base, "F", horizon=10)
    r2 = validate.decile_backtest(shifted, "F", horizon=10)
    check("十分位毛價差也不受共同報酬影響",
          abs(r1["單期毛價差"] - r2["單期毛價差"]) < 1e-6,
          "{:+.4f} vs {:+.4f}".format(r1["單期毛價差"], r2["單期毛價差"]))


# ------------------------------------------------------------------ 十分位回測
def test_decile():
    print("\n十分位回測")
    # 構造已知價差：最高分位報酬 +5%，最低分位 -5%，其餘 0
    rng = np.random.default_rng(5)
    dates = pd.bdate_range("2020-01-01", periods=300)
    rows = []
    for d in dates:
        f = rng.permutation(np.arange(200))          # 因子 = 0..199 的排列
        fwd = np.zeros(200)
        fwd[f >= 180] = 5.0                          # 前 10%
        fwd[f < 20] = -5.0                           # 後 10%
        for i in range(200):
            rows.append((d, "S{:03d}".format(i), float(f[i]), fwd[i]))
    p = pd.DataFrame(rows, columns=["date", "code", "F", "fwd10"])
    r = validate.decile_backtest(p, "F", horizon=10, cost=0.0)
    check("已知 ±5% 構造 -> 毛價差 = 10%",
          abs(r["單期毛價差"] - 10.0) < 1e-6, "實得 {:.4f}%".format(r["單期毛價差"]))
    check("成本設 0 時 淨 = 毛",
          abs(r["單期淨價差"] - r["單期毛價差"]) < 1e-6)

    # 成本扣除依實測換手率縮放：扣除額 = cost × 100 × 換手率 × 2 條腿
    r2 = validate.decile_backtest(p, "F", horizon=10, cost=0.006)
    expect = 0.006 * 100 * (r2["換手率"] / 100) * 2
    # 容差：回傳的換手率四捨五入到小數一位，換算後最大誤差 0.6%×0.001×2×100 = 0.0012
    check("成本扣除 = 0.6% × 實測換手率 × 2 條腿",
          abs((r2["單期毛價差"] - r2["單期淨價差"]) - expect) < 2e-3,
          "扣除 {:.4f}，換手率 {:.1f}%".format(
              r2["單期毛價差"] - r2["單期淨價差"], r2["換手率"]))
    # 因子每日隨機重排 -> 名單幾乎全換，換手率應接近 100%
    check("隨機因子的換手率應接近 100%", r2["換手率"] > 85,
          "{:.1f}%".format(r2["換手率"]))
    # 構造的關係是完美單調的（前 10% 全 +5%，後 10% 全 -5%）
    check("單調性指標為正", r2["單調性"] > 0.5, "{:+.2f}".format(r2["單調性"]))

    # 方向：因子反號 -> 價差反號
    p3 = p.copy(); p3["F"] = -p3["F"]
    r3 = validate.decile_backtest(p3, "F", horizon=10, cost=0.0)
    check("因子取負 -> 價差符號相反",
          abs(r3["單期毛價差"] + r["單期毛價差"]) < 1e-6,
          "{:+.2f} vs {:+.2f}".format(r3["單期毛價差"], r["單期毛價差"]))


# ------------------------------------------------------------------ Newey-West
def test_newey_west():
    print("\nNewey-West 與自助法")
    rng = np.random.default_rng(23)
    x = rng.standard_normal(500) + 0.5
    mu, t_nw, n = validate.newey_west_t(x, lag=0)
    t_naive = x.mean() / (x.std(ddof=0) / np.sqrt(len(x)))
    check("lag=0 時 NW t 等於一般 t",
          abs(t_nw - t_naive) < 1e-9, "{:.4f} vs {:.4f}".format(t_nw, t_naive))

    # 正自相關序列：NW 應該放大不確定性 -> |t| 變小
    e = rng.standard_normal(500)
    ar = np.zeros(500)
    for i in range(1, 500):
        ar[i] = 0.7 * ar[i - 1] + e[i]
    ar = ar + 0.3
    _, t0, _ = validate.newey_west_t(ar, lag=0)
    _, t9, _ = validate.newey_west_t(ar, lag=9)
    check("正自相關下 NW 修正後 |t| 應小於未修正",
          abs(t9) < abs(t0), "lag0 t={:.2f}  lag9 t={:.2f}".format(t0, t9))

    # 小樣本防呆：NW 變異數不得低於 iid
    small = rng.standard_normal(40)
    _, ts, _ = validate.newey_west_t(small, lag=9)
    _, t0s, _ = validate.newey_west_t(small, lag=0)
    check("小樣本下 NW 不會放大 t（變異數下限保護）", abs(ts) <= abs(t0s) + 1e-9,
          "lag9 {:.3f} <= lag0 {:.3f}".format(abs(ts), abs(t0s)))

    check("樣本數不足時拒絕給 t 值",
          np.isnan(validate.newey_west_t(rng.standard_normal(10), lag=2)[1]))

    # 自助法：對平均為 0 的序列不應拒絕虛無假設
    p_null = validate.bootstrap_p(rng.standard_normal(300), n_boot=2000)
    check("自助法對零均值序列 p 值偏大", p_null > 0.10, "p={:.3f}".format(p_null))
    p_alt = validate.bootstrap_p(rng.standard_normal(300) + 0.5, n_boot=2000)
    check("自助法對明顯非零序列 p 值極小", p_alt < 0.01, "p={:.3f}".format(p_alt))


# ------------------------------------------------------------------ 非重疊抽樣
def test_nonoverlap():
    print("\n非重疊抽樣")
    p = synth(signal=0.2, n_days=400, seed=29)
    a = validate.daily_ic(p, "F", horizon=10)
    b = validate.daily_ic(p, "F", horizon=10, non_overlapping=True)
    check("非重疊抽樣期數約為重疊的 1/10",
          abs(len(b) - len(a) / 10) <= 1, "{} vs {}".format(len(b), len(a)))
    check("兩者平均 IC 接近（抽樣不應改變估計值）",
          abs(a.mean() - b.mean()) < 0.03,
          "{:+.4f} vs {:+.4f}".format(a.mean(), b.mean()))


# ------------------------------------------------------------------ 除權息調整
def test_adjust():
    print("\n除權息調整鏈")
    # 100 -> 110（+10%）-> 除息 10 元，參考價 100，收 105（相對參考價 +5%）
    q = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
        "code": ["A"] * 3,
        "close": [100.0, 110.0, 105.0],
        "ref_price": [np.nan, np.nan, 100.0],
    })
    g = q.groupby("code", sort=False)
    prev = g["close"].shift(1)
    base = q["ref_price"].where(q["ref_price"].notna(), prev)
    ret = q["close"] / base - 1
    check("除息日報酬以參考價為分母",
          abs(ret.iloc[2] - 0.05) < 1e-12, "實得 {:+.4%}".format(ret.iloc[2]))
    unadj = q["close"].iloc[2] / q["close"].iloc[1] - 1
    check("未調整版本會低估（-4.55% vs +5.00%）",
          abs(unadj + 0.0454545) < 1e-6, "未調整 {:+.4%}".format(unadj))
    total = (1 + ret.fillna(0)).cumprod().iloc[2]
    check("累積總報酬指數 = 1.10 × 1.05 = 1.155",
          abs(total - 1.155) < 1e-12, "實得 {:.6f}".format(total))


# ------------------------------------------------------------------ 檔位
def test_tick_roundtrip():
    print("\n檔位與漲跌停（邊界窮舉）")
    bad = []
    for pc in np.arange(5.0, 60.0, 0.05):
        up, dn = factors.limit_prices([pc])
        up, dn = float(up[0]), float(dn[0])
        if not (dn < pc < up):
            bad.append(pc)
        if up > pc * 1.10 + 1e-9 or dn < pc * 0.90 - 1e-9:
            bad.append(pc)           # 不得超出 ±10%
    check("1100 個價位的漲跌停都落在 ±10% 內且包住前收",
          not bad, "異常 {} 個".format(len(bad)))


# ------------------------------------------------------------------ walk-forward
def test_walk_forward():
    """樣本外驗證函式本身的正確性。

    新寫的評估函式一定要先用已知答案驗過再拿去看真實資料 ——
    否則分不清「結果是負的」和「程式算錯了」。
    """
    print("\nWalk-forward 樣本外驗證")

    def mk(signal, seed, n_days=1200, n=250):
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range("2016-01-01", periods=n_days)
        rows = []
        for dt in dates:
            a = rng.standard_normal(n); b = rng.standard_normal(n)
            fwd = (signal * a + np.sqrt(max(1e-9, 1 - signal ** 2))
                   * rng.standard_normal(n)) * 3
            for i in range(n):
                rows.append((dt, "S{:03d}".format(i), a[i], b[i], fwd[i]))
        return pd.DataFrame(rows, columns=["date", "code", "A", "B", "fwd10"])

    r = validate.walk_forward(mk(0.25, 3), ["A", "B"], horizon=10, train=40, cost=0.0)
    check("已知正訊號 -> 樣本外毛價差顯著為正", r["單期毛價差"] > 1.0 and r["t值_NW"] > 5,
          "毛 {:+.3f}%  t={:+.1f}".format(r["單期毛價差"], r["t值_NW"]))

    r2 = validate.walk_forward(mk(-0.25, 9), ["A", "B"], horizon=10, train=40,
                               cost=0.0, method="ic_weight")
    check("已知反訊號 -> IC 加權能自動翻正", r2["單期毛價差"] > 1.0,
          "毛 {:+.3f}%".format(r2["單期毛價差"]))

    r3 = validate.walk_forward(mk(-0.25, 9), ["A", "B"], horizon=10, train=40,
                               cost=0.0, method="equal")
    check("已知反訊號 -> 等權（不翻轉）維持負值", r3["單期毛價差"] < -1.0,
          "毛 {:+.3f}%".format(r3["單期毛價差"]))

    r4 = validate.walk_forward(mk(0.0, 21), ["A", "B"], horizon=10, train=40, cost=0.0)
    check("純雜訊 -> 毛價差接近 0", abs(r4["單期毛價差"]) < 0.3,
          "毛 {:+.3f}%".format(r4["單期毛價差"]))

    r5 = validate.walk_forward(mk(0.25, 3).head(3000), ["A", "B"], horizon=10, train=40)
    check("期數不足時拒絕給結論", "樣本不足" in str(r5.get("判定", "")))


if __name__ == "__main__":
    print("=" * 70)
    print("統計計算正確性驗證（以已知答案的合成資料反推）")
    print("=" * 70)
    test_ic()
    test_neutralize()
    test_decile()
    test_newey_west()
    test_nonoverlap()
    test_adjust()
    test_tick_roundtrip()
    test_walk_forward()
    print("\n" + "=" * 70)
    print("通過 {} 項，失敗 {} 項".format(len(PASS), len(FAIL)))
    if FAIL:
        print("失敗: " + ", ".join(FAIL))
    print("=" * 70)
    sys.exit(1 if FAIL else 0)
