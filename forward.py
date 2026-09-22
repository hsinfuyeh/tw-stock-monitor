"""前瞻實測（「短線交易與研究規範 v1」第 3、5、7 節）。

回測已經被翻過八輪，歷史資料再怎麼調都只是在背過去的雜訊。規範的決定是：
**現行設定凍結，從 2026-09-22 起用真的、沒被看過的資料往前走**，
同時追蹤 4 個「紙上臂」，每臂只跟 Arm 0 差一件事，才看得出差異是哪件事造成的。

    Arm 0 現行版    網站預設（停損 3×ATR、目標淨賺 5%、最長 20 日、波動門檻 2.5%）
    Arm 1 不設目標  只差：目標關閉 —— 量出 5% 目標的實際代價
    Arm 2 短期反轉  完全不同的選股（M4）：超跌、成交量大、大盤不是空頭
    Arm 3 10 日版   只差：最長持有 10 個交易日 —— 對齊「兩週」

成績用**有資金限制的組合**算，不是把清單 20 檔逐筆平均（規範第 3 節）：
    最多同時 3 檔、單筆風險 0.5%、每天最多新增 1 檔、單檔 ≤ 20%、同產業 ≤ 2 檔，
    處置股與注意股不買。部位 = 淨值 × 0.5% ÷ 停損距離。
第 8 輪用 18 年歷史驗證過這個組合模擬的寫法（逐日盯市與逐筆淨報酬對帳一致）。

判定（規範第 7 節，事前寫死，任何參數改動都讓該臂重新起算）：
    中期 120 個交易日：對 0050 超額 ≤ −0.3% 且 t ≤ −2 -> 該臂停用
    最終 250 個交易日：超額 > 0、t ≥ 2.5、最大回落 ≤ 15%（Arm 0 另加達標率 ≥ 55%）
"""
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

import risk_lists
import shortterm as S

ROOT = Path(__file__).parent
SNAP = ROOT / "snapshots"          # Arm 0 沿用原本的位置；其他臂放子資料夾
START = "2026-09-22"               # 規範 v1 的起算日（第一個訊號日）
START9 = "2026-09-23"              # 第 9 輪新臂（Arm 4–8）的起算日

ARMS = [
    dict(key="arm0", name="Arm 0 現行版", kind="prd", params={},
         desc="網站預設：停損 3×ATR、目標淨賺 5%、最長 20 日"),
    dict(key="arm1", name="Arm 1 不設目標", kind="prd", params={"use_target": 0},
         desc="跟 Arm 0 只差一件事：不設目標價"),
    dict(key="arm2", name="Arm 2 短期反轉", kind="m4",
         params={"hold_days": 10, "atr_mult": 3.0, "use_target": 0},
         desc="超跌（5 日報酬 z ≤ −2.5）、成交額前 30%、大盤不是空頭；持有 10 日、不設目標"),
    dict(key="arm3", name="Arm 3 10 日版", kind="prd", params={"hold_days": 10},
         desc="跟 Arm 0 只差一件事：最長持有 10 個交易日"),
    # 第 9 輪（RESEARCH_LOG 事前登記）：五個改進方案，2026-09-23 起算。
    # 同時跑的臂變多，靠運氣過關的機會變大，所以最終判定要 t ≥ 3（其他臂 2.5）。
    dict(key="arm4", name="Arm 4 只挑大型股", kind="prd", params={"big_n": 150},
         start=START9, final_t=3.0,
         desc="跟 Arm 0 只差一件事：只挑成交額全市場前 150 名的股票"),
    dict(key="arm5", name="Arm 5 總分 70 以上", kind="prd", params={"min_score": 70},
         start=START9, final_t=3.0,
         desc="跟 Arm 0 只差一件事：總分至少 70（四個條件至少中三個左右）"),
    dict(key="arm6", name="Arm 6 停損太寬不買", kind="prd", params={"skip_wide": 1},
         start=START9, final_t=3.0,
         desc="跟 Arm 0 只差一件事：3×ATR 超過 10% 的不買，而不是把停損硬夾在 10%"),
    dict(key="arm7", name="Arm 7 只看月營收", kind="prd",
         params={"w_inst": 0, "w_brk": 0, "w_ma": 0, "use_target": 0},
         start=START9, final_t=3.0,
         desc="只用月營收條件選股、不設目標價（最長 20 日）"),
    dict(key="arm8", name="Arm 8 模型選股", kind="prd", params={"use_model": 1},
         start=START9, final_t=3.0,
         desc="用逐年重訓的模型估「20 天後贏 0050」的機率，≥ 50% 才選；停損與目標同 Arm 0"),
]
ARM = {a["key"]: a for a in ARMS}

# 規範第 5 節（部位與風險上限）
RULES = dict(max_hold=3, risk=0.005, new_per_day=1, cap=0.20, ind_max=2,
             # 第 4 節：買進金額 > 日均成交額 1% 不買。組合是用比例算的，
             # 要換成金額才能檢查，所以假設一個本金（學費規模下幾乎不會碰到這條）。
             capital=1_000_000, adtv_max=0.01)
RISK_LOOKBACK = 10     # 補算／更新名單時最多回頭幾個交易日（再舊的公告清單已經查不到）
# 規範第 7 節（判定門檻）
MID, FINAL = 120, 250
KEEP_DAYS = 30                     # 頁面上逐日清單最多保留幾天（避免檔案一年後太大）
# 逐日清單在網頁上用得到的欄位。Arm 1、3 的選股跟 Arm 0 一樣，入選理由只存在 Arm 0。
ROW_KEEP = ("code", "name", "kind", "ind", "score", "close", "flags", "status", "entry",
            "entry_date", "exit", "exit_date", "days", "ret", "stop_used", "target_used")


def arm_dir(key):
    return SNAP if key == "arm0" else SNAP / key


def arm_params(key):
    return S.params(**ARM[key]["params"])


# --------------------------------------------------------------------- 選股
def pick_m4(day):
    """Arm 2：短期反轉（第 1、7 輪研究的 M4，設定「z≤−2.5 大盤非down=要」）。

    範圍跟研究一致：個股、20 日成交額中位數 ≥ 5,000 萬、收盤 ≥ 10 元；
    其中成交額排名前 30%、5 日報酬 z ≤ −2.5、大盤不是 down，依 z 由低到高取前 10。
    """
    x = day[(day["kind"] == "個股") & (day["adtv20"] >= 5e7) & (day["close"] >= 10)]
    if not len(x):
        return x
    rk = x["adtv20"].rank(pct=True, ascending=False)
    x = x[(rk <= 0.3) & (x["ret5_z"] <= -2.5) & (x["regime"] != "down")]
    return x.sort_values("ret5_z").head(10)


def picks(d, key, date):
    """某一臂在某個訊號日的清單（依排序），附參考價位。"""
    p = arm_params(key)
    if ARM[key]["kind"] == "m4":
        day = d[d["date"] == date]
        rows = pick_m4(day).copy()
        if not len(rows):
            return rows
        stop, target = S.ref_prices(rows["close"], rows["atrp14"], p)
        rows = rows.assign(stop=stop, target=target, score=np.nan)
        rows["why"] = [["5 日報酬 {:+.1f}%，比自己過去一年的平常低 {:.1f} 個標準差".format(
            r.ret5 * 100, -r.ret5_z)] for r in rows.itertuples()]
        return rows
    return S.today(d, p, date)


def _row(r, flags):
    code = str(r["code"])
    return {"code": code, "name": r["name"], "kind": r["kind"],
            "ind": r.get("ind") if isinstance(r.get("ind"), str) else None,
            "score": S._r(r.get("score"), 1), "close": float(r["close"]),
            "atrp14": S._r(r.get("atrp14"), 5),
            "stop": float(r["stop"]), "target": S._r(r["target"]),
            "adtv20": S._r(r.get("adtv20"), 0),
            "why": r["why"], "flags": flags.get(code, [])}


def flag_map(risk):
    """代號 -> 排除理由。"""
    out = {}
    for c in risk.get("disposal") or []:
        out.setdefault(c, []).append("處置股")
    for c in risk.get("attention") or []:
        out.setdefault(c, []).append("注意股")
    for c in risk.get("full_delivery") or []:
        out.setdefault(c, []).append("全額交割")
    return out


def apply_risk(snap, risk):
    """用新拿到的名單重標一份快照（隔天回頭補上前一天還沒公布的注意股）。"""
    fm = flag_map(risk)
    for r in snap["top"]:
        r["flags"] = fm.get(r["code"], [])
    snap["risk"] = {k: risk.get(k) for k in risk_lists.STATUS_KEYS}
    return snap


def snapshot(d, key, date, risk, backfilled=False):
    rows = picks(d, key, date)
    fm = flag_map(risk)
    return {"date": str(date)[:10], "arm": key, "params": arm_params(key),
            "backfilled": backfilled,
            "risk": {k: risk.get(k) for k in risk_lists.STATUS_KEYS},
            "top": [_row(r, fm) for _, r in rows.iterrows()]}


# --------------------------------------------------------------------- 逐筆追蹤
def track_trade(x, r, p):
    """一筆的實際走勢。規則跟 shortterm.backtest 完全一樣：
    隔天開盤進場（一字漲停買不到）、停損與目標用**成交價**重算、
    跳空以開盤價成交、同一天碰到兩邊算停損、最長 hold_days 天，含除權息、扣成本。
    x：該股訊號日之後的日線（依日期排序）。"""
    out = dict(status="等待進場", entry_date=None, entry=None, exit_date=None, exit=None,
               days=0, ret=None, stop_used=None, target_used=None, path=None)
    if x is None or not len(x):
        return out
    o, h, lo, c, k = (x[col].to_numpy(dtype=float) for col in ("open", "high", "low", "close", "k"))
    if h[0] == lo[0] and o[0] >= r["close"] * 1.095:
        out.update(status="買不到（一字漲停）", entry_date=str(x["date"].iat[0])[:10])
        return out
    entry = o[0]
    if r.get("atrp14") is not None:            # 用成交價重算（舊快照沒有 ATR 時沿用存的價位）
        st, tg = S.ref_prices(np.array([entry]), np.array([r["atrp14"]]), p)
        stop, target = float(st[0]), float(tg[0])
    else:
        stop, target = r["stop"], r["target"]
    tgt = target if target is not None and np.isfinite(target) else np.inf
    hold = int(p["hold_days"])
    status, xi, px = None, None, None
    for j in range(min(hold, len(x))):
        if o[j] <= stop and j > 0:
            status, xi, px = "停損出場", j, o[j]
        elif lo[j] <= stop:
            status, xi, px = "停損出場", j, min(stop, o[j]) if j > 0 else stop
        elif o[j] >= tgt and j > 0:
            status, xi, px = "達到目標", j, o[j]
        elif h[j] >= tgt:
            status, xi, px = "達到目標", j, tgt
        elif j == hold - 1:
            status, xi, px = "時間到出場", j, c[j]
        if status:
            break
    if status is None:
        status, xi, px = "持有中", len(x) - 1, c[-1]
    # 逐日毛報酬路徑（組合模擬用）：進場日以開盤買、之後收盤盯市、出場日用出場價
    ca = c * k
    path = []
    for j in range(xi + 1):
        a = entry * k[0] if j == 0 else ca[j - 1]
        b = px * k[j] if j == xi else ca[j]
        path.append((str(x["date"].iat[j])[:10], b / a - 1))
    ret = (px * k[xi] / (entry * k[0]) - 1) * 100 - S.COST
    out.update(status=status, entry_date=str(x["date"].iat[0])[:10], entry=S._r(entry),
               exit_date=None if status == "持有中" else str(x["date"].iat[xi])[:10],
               exit=S._r(px), days=int(xi + 1), ret=S._r(ret),
               stop_used=S._r(stop), target_used=S._r(target), path=path)
    return out


def track_snapshot(d, snap, by_code=None):
    date = pd.Timestamp(snap["date"])
    p = S.params(**{k: v for k, v in snap.get("params", {}).items() if k in S.DEFAULTS})
    rows = []
    for r in snap["top"]:
        x = None
        if by_code is not None and r["code"] in by_code:
            g = by_code[r["code"]]
            x = g[g["date"] > date].reset_index(drop=True)
        rows.append(dict(r, **track_trade(x, r, p)))
    return rows


# --------------------------------------------------------------------- 組合模擬
def portfolio(days, snaps_tracked, bench, start, bench_open=None):
    """有資金限制的紙上組合。snaps_tracked：[(訊號日, [追蹤後的 row…])]，依日期排序。

    跟第 8 輪 round8_sim.simulate 同一套規則，另外加上規範第 4 節的
    同產業 ≤ 2 檔、處置股與注意股不買。
    """
    cal = [x for x in days if x > start]
    by_entry = {}
    for sd, rows in snaps_tracked:
        for i, r in enumerate(rows):
            if r.get("entry_date"):
                by_entry.setdefault(r["entry_date"], []).append((sd, i, r))
    eq, cash, open_, trades, expo = [], 1.0, [], [], []
    for D in cal:
        eq_prev = eq[-1][1] if eq else 1.0
        new = 0
        for sd, i, r in by_entry.get(D, ()):
            if new >= RULES["new_per_day"] or len(open_) >= RULES["max_hold"]:
                break
            skip = None
            if not str(r["status"]).endswith("出場") and r["status"] not in ("持有中", "達到目標"):
                skip = r["status"]
            elif r.get("flags"):
                skip = "、".join(r["flags"])
            elif any(p["code"] == r["code"] for p in open_):
                skip = "已持有"
            elif r.get("ind") and sum(p["ind"] == r["ind"] for p in open_) >= RULES["ind_max"]:
                skip = "同產業已有 {} 檔".format(RULES["ind_max"])
            elif r.get("adtv20") and (eq_prev * RULES["capital"] * min(
                    RULES["risk"] / max((r["entry"] - r["stop_used"]) / r["entry"], 1e-9), RULES["cap"])
                    > RULES["adtv_max"] * r["adtv20"]):
                skip = "金額超過日均成交額 1%"
            if skip:
                trades.append(dict(signal=sd, code=r["code"], name=r["name"], taken=False,
                                   why=skip))
                continue
            dist = (r["entry"] - r["stop_used"]) / r["entry"] if r["entry"] else np.nan
            if not np.isfinite(dist) or dist <= 0:
                continue
            notion = min(eq_prev * RULES["risk"] / dist, eq_prev * RULES["cap"], cash)
            cash -= notion
            path = {dd: g for dd, g in (r["path"] or [])}
            open_.append(dict(code=r["code"], ind=r.get("ind"), notion=notion, val=notion,
                              path=path, row=r, sd=sd, eq_in=eq_prev))
            new += 1
        still = []
        for pos in open_:
            if D in pos["path"]:
                pos["val"] *= 1 + pos["path"][D]
            r = pos["row"]
            if r.get("exit_date") == D:           # 出場：扣來回成本
                fin = pos["val"] - pos["notion"] * S.COST / 100
                cash += fin
                # 同一段期間的 0050（進場日開盤買、出場日收盤賣，扣 ETF 成本），跟回測頁同一套
                bo = (bench_open if bench_open is not None else bench).get(r["entry_date"])
                bc = bench.get(D)
                bret = (bc / bo - 1) * 100 - S.ETF_COST if bo and bc else None
                trades.append(dict(signal=pos["sd"], code=r["code"], name=r["name"], taken=True,
                                   entry_date=r["entry_date"], exit_date=D, status=r["status"],
                                   ret=r["ret"], bench=S._r(bret),
                                   excess=S._r(r["ret"] - bret) if bret is not None else None,
                                   weight=S._r(pos["notion"] / pos["eq_in"] * 100),
                                   pnl_cap=S._r((fin - pos["notion"]) / pos["eq_in"] * 100, 3)))
            else:
                still.append(pos)
        open_ = still
        inv = sum(p["val"] for p in open_)
        eq.append((D, cash + inv))
        expo.append(inv / (cash + inv) if cash + inv > 0 else 0.0)
    for pos in open_:
        r = pos["row"]
        trades.append(dict(signal=pos["sd"], code=r["code"], name=r["name"], taken=True,
                           entry_date=r["entry_date"], exit_date=None, status="持有中",
                           ret=r["ret"], weight=S._r(pos["notion"] / pos["eq_in"] * 100),
                           pnl_cap=S._r((pos["val"] - pos["notion"]) / pos["eq_in"] * 100, 3)))
    # 0050：起算日收盤買進持有
    b = bench.reindex([start] + cal).ffill()
    b = (b / b.iloc[0]).to_numpy()[1:] if len(b) > 1 else np.array([])
    return eq, b, trades, expo


def summarize(eq, b, trades, key, expo=()):
    """規範第 3、7 節的指標與目前狀態。

    「對 0050 超額」照規範是**逐筆**：組合實際買的每一筆，減掉同一段期間 0050 的報酬，再取平均
    （跟回測頁的 −0.44% 同一個尺度，中期門檻 −0.3%、t ≥ 2.5 也是依這個尺度訂的）。
    t 值跟回測一樣：同一個訊號日的交易先平均成一個觀察值，Newey-West、lag = 持有天數。

    組合總報酬 vs 0050 總報酬（port / bench / gap）另外列出來給人看，但不拿來判定：
    組合平常只有一成多資金在股票上，大盤漲時一定落後，那是部位規模不是選股好壞。"""
    n = len(eq)
    taken = [t for t in trades if t.get("taken")]
    closed = [t for t in taken if t.get("exit_date")]
    out = {"days": n, "entered": len(taken), "closed": len(closed),
           "open": len(taken) - len(closed),
           "skipped": sum(1 for t in trades if not t.get("taken"))}
    if closed:
        out["hit"] = S._r(sum(t["status"] == "達到目標" for t in closed) / len(closed) * 100, 1)
        out["worst_cap"] = S._r(min(t["pnl_cap"] for t in closed), 2)
    if len(expo):
        # 平均持股比重。規範的「學費」規模（3 檔 × 每檔約 5%）平常只有約 15% 在市場上，
        # 跟 100% 持有的 0050 比，多頭時一定落後 —— 頁面要把這個數字放在成績旁邊。
        out["expo"] = S._r(float(np.mean(expo)) * 100, 1)
    ex = [t for t in closed if t.get("excess") is not None]
    if ex:
        out["excess"] = S._r(float(np.mean([t["excess"] for t in ex])))
        out["n_excess"] = len(ex)
        sig = pd.DataFrame(ex).groupby("signal")["excess"].mean()
        if len(sig) >= 20:                   # 太少時 NW 的變異數估計不可靠
            import validate
            out["t"] = S._r(float(validate.newey_west_t(
                sig.to_numpy(), lag=int(arm_params(key)["hold_days"]))[1]))
    if n:
        e = np.array([v for _, v in eq])
        out["port"] = S._r((e[-1] - 1) * 100)
        out["bench"] = S._r((b[-1] - 1) * 100) if len(b) else None
        out["gap"] = S._r(out["port"] - out["bench"]) if out["bench"] is not None else None
        peak = np.maximum.accumulate(np.concatenate([[1.0], e]))[1:]
        out["mdd"] = S._r(((e / peak) - 1).min() * 100)
        if n >= 10:
            e0 = np.concatenate([[1.0], e])
            r10 = e0[10:] / e0[:-10] - 1
            out["pos10"] = S._r((r10 > 0).mean() * 100, 1)
    out["status"] = status(out, key)
    return out


def status(m, key):
    n = m.get("days", 0)
    if n < MID:
        return {"level": "todo", "label": "累積中",
                "note": ("還沒有紀錄：起算日當天收盤後產生第一份清單，隔天開盤才開始「買進」。" if n == 0 else
                         "天數太少時的成績大多是運氣，要到第 {} 天才做中期檢查。".format(MID))}
    ex, t = m.get("excess"), m.get("t")
    if n < FINAL:
        if ex is not None and t is not None and ex <= -0.3 and t <= -2:
            return {"level": "fail", "label": "停用",
                    "note": "中期檢查未過：對 0050 {:+.2f}%、t = {:.2f}（規範：≤ −0.3% 且 t ≤ −2 就停用）".format(ex, t)}
        return {"level": "watch", "label": "中期通過",
                "note": "沒有觸發停用條件，繼續累積到第 {} 天做最終判定。".format(FINAL)}
    need_t = ARM[key].get("final_t", 2.5) if key in ARM else 2.5
    ok = (ex is not None and ex > 0 and t is not None and t >= need_t
          and m.get("mdd") is not None and m["mdd"] >= -15
          and (key != "arm0" or (m.get("hit") or 0) >= 55))
    return ({"level": "ok", "label": "成功", "note": "達到規範第 7 節的全部門檻。"} if ok else
            {"level": "fail", "label": "未通過", "note": "第 {} 天最終判定未達規範第 7 節門檻。".format(n)})


# --------------------------------------------------------------------- 主流程
def load_snaps(key):
    snaps = {}
    folder = arm_dir(key)
    if folder.exists():
        for f in folder.glob("*.json"):
            snaps[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    return snaps


def compute(d, risk_today, risk_fn=risk_lists.fetch):
    """各臂今天的快照、缺日補算、追蹤與組合成績。回傳 (要寫的快照, forward.json)。

    risk_fn(日期)：回頭抓某天的處置／注意／全額交割名單。用在兩個地方：
      補算缺日 —— 不再一律標「不知道」（那樣等於那天完全不排除）
      更新前幾天 —— 產出時注意股可能還沒公布，之後回頭補上標記
    這不算偷看未來：這些名單在「隔天開盤進場」之前就已經公布了。
    """
    days = [str(x)[:10] for x in np.sort(d["date"].unique())]
    today = days[-1]
    by_code = {c: g.sort_values("date") for c, g in d.groupby("code", sort=False)}
    bench = d[d["code"] == "0050"].set_index(d[d["code"] == "0050"]["date"].astype(str).str[:10])
    bench_open = (bench["open"] * bench["k"]).to_dict()
    bench = bench["close"] * bench["k"]
    new, arms_out = {}, []
    recent = set([x for x in days if START <= x < today][-RISK_LOOKBACK:])
    # （新臂起算日較晚，下面每一臂各用自己的 start）
    rcache = {}

    def risk_of(m):
        if m not in rcache:
            rcache[m] = risk_lists.unknown(m)
            if m in recent and risk_fn is not None:
                try:
                    rcache[m] = risk_fn(m)
                except Exception as e:          # 抓不到就照實標 unknown，不中斷產出
                    print("  名單 {} 抓不到：{}".format(m, e))
        return rcache[m]

    for a in ARMS:
        key = a["key"]
        start = a.get("start", START)
        snaps = load_snaps(key)
        new[key] = {}
        if today >= start:
            new[key][today] = snapshot(d, key, pd.Timestamp(today), risk_today)
        # 缺日補算：起算日之後、今天之前沒有快照的交易日（例如那天 CI 沒跑）
        for m in [x for x in days if start <= x < today and x not in snaps]:
            new[key][m] = snapshot(d, key, pd.Timestamp(m), risk_of(m), backfilled=True)
        # 前幾天產出時名單還沒確定的，回頭更新標記
        for m in sorted(recent):
            sn = snaps.get(m)
            if sn is None or m in new[key] or risk_lists.settled(sn.get("risk")):
                continue
            r = risk_of(m)
            if risk_lists.settled(r):
                new[key][m] = dict(apply_risk(sn, r), risk_refreshed=True)
        snaps.update(new[key])
        order = sorted(snaps)
        tracked = [(sd, track_snapshot(d, snaps[sd], by_code)) for sd in order]
        live = [(sd, rows) for sd, rows in tracked if sd >= start]
        eq, b, trades, expo = portfolio(days, live, bench, start, bench_open)
        m = summarize(eq, b, trades, key, expo)
        # Arm 2 的影子：同一份清單「不停損」的逐筆平均（規範第 7 節）
        shadow = None
        if key == "arm2" and live:
            sp = S.params(**dict(a["params"], atr_mult=99))       # 99 倍 ATR ＝ 不停損
            rets = []
            for sd, rows in live:
                for r in rows:
                    g = by_code.get(r["code"])
                    x = g[g["date"] > pd.Timestamp(sd)].reset_index(drop=True) if g is not None else None
                    ret = track_trade(x, r, sp)["ret"]
                    if ret is not None:
                        rets.append(ret)
            shadow = {"n": len(rets), "avg": S._r(float(np.mean(rets))) if rets else None}
        days_list = []
        keep = ROW_KEEP + (("why",) if key in ("arm0", "arm2") else ())
        for sd, rows in tracked[::-1][:KEEP_DAYS]:
            rows = [{f: r.get(f) for f in keep} for r in rows]
            done = [r for r in rows if r.get("ret") is not None]
            days_list.append({
                "date": sd, "pre": sd < start, "backfilled": bool(snaps[sd].get("backfilled")),
                "risk": snaps[sd].get("risk"), "n": len(rows),
                "waiting": sum(r["status"] == "等待進場" for r in rows),
                "open": sum(r["status"] == "持有中" for r in rows),
                "avg": S._r(float(np.mean([r["ret"] for r in done]))) if done else None,
                "rows": rows})
        arms_out.append(dict(key=key, name=a["name"], desc=a["desc"], start=start,
                             final_t=a.get("final_t", 2.5),
                             params=arm_params(key), metrics=m, shadow=shadow,
                             equity=[[dd, S._r(v, 5), S._r(bb, 5)]
                                     for (dd, v), bb in zip(eq, b)],
                             trades=trades[-200:], days_list=days_list))
    fwd = {"date": today, "start": START, "rules": RULES, "mid": MID, "final": FINAL,
           "stage": STAGE, "kill": KILL, "risk_today": risk_today, "arms": arms_out}
    return new, fwd


def persist():
    """快照檔只由 CI 寫（它會 commit 回 repo）。本機產網頁時照樣算進 forward.json，
    但不寫檔 —— 否則本機跟 CI 各寫一份同名檔案，下次 git pull 就會衝突。
    本機真的要寫（例如 CI 壞了要手動補）就設 FORWARD_PERSIST=1。"""
    return bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("FORWARD_PERSIST"))


def write(out, new, fwd):
    for key, snaps in (new.items() if persist() else ()):
        folder = arm_dir(key)
        folder.mkdir(parents=True, exist_ok=True)
        for day, snap in snaps.items():
            f = folder / (day + ".json")
            # 補算的不蓋掉當天即時產生的；名單更新過的要蓋掉
            if day == fwd["date"] or not f.exists() or snap.get("risk_refreshed"):
                f.write_text(json.dumps(S._json_safe(snap), ensure_ascii=False, indent=1),
                             encoding="utf-8")
    (out / "short").mkdir(parents=True, exist_ok=True)
    (out / "short" / "forward.json").write_text(
        json.dumps(S._json_safe(fwd), ensure_ascii=False, separators=(",", ":"),
                   allow_nan=False), encoding="utf-8")


# 規範第 10 節：資金階段與立即停止條件。頁面上照這份顯示。
STAGE = {"current": "0", "name": "紙上（不下單）",
         "stages": [["0", "紙上", "現在起（預設）", "不下單"],
                    ["0b", "驗證期實盤", "你選擇不等驗證、仍要下單，且完全遵守規範第 4–6 節", "可投資資產 10%"],
                    ["1", "小額實盤", "120 個交易日中期檢查通過，且違規交易每月 ≤ 1 筆", "可投資資產 20%"],
                    ["2", "正常實盤", "250 個交易日達規範第 7 節成功標準", "由你決定"]]}
KILL = ["實盤帳戶從高點回落達 15%", "連續 5 筆停損", "單筆虧損超過本金 1%",
        "一個月內違規 3 次以上"]
