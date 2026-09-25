"""月營收擷取（公開資訊觀測站 MOPS 的月報表）。

為什麼需要這個 —— 它是「剔除價值陷阱」那一層的核心：

    殖利率 = 股利 / 股價。股價暴跌會讓殖利率飆高。
    我的統計層會把這種股票評為高分，但那不是便宜，是公司在爛掉。
    加上營收趨勢才分得出「便宜的好公司」與「價值陷阱」。

資料來源：MOPS 每月一頁的「上市公司營業收入統計表」，一頁就是當月<b>全部</b>
上市公司（含 KY 公司），一個月一次請求，2007 年起都有。

    舊版用 FinMind 逐檔抓，而且只抓「最近 30 天成交額前 570 檔」。
    那等於只有活到今天、現在還熱門的公司才有歷史營收 —— 存活者偏誤。
    實測 2019 年後，有抓營收的股票之後 20 天比全體平均 +0.37%，
    沒抓的 −1.42%：營收訊號的回測好看，有一大塊是這個偏誤給的。
    而且快取抓過就不再更新，9/16 之後的新月份永遠進不來 ——
    超過 75 天會變缺值，營收那 25 分會在某一天整批靜靜歸零。

    月報表是「那個月當時有申報的所有公司」，後來下市的也在，
    所以用它建出來的歷史沒有存活者偏誤。

年增率用同一頁的「去年當月營收」算，不用自己拿 12 個月前那筆去除：
2013 年起上市公司改用 IFRS 合併營收，同一家公司前後的數字不是同一個基準，
直接相除會在 2013 年製造出一整年的假成長。頁面上的去年同月是公司在同一個
基準下申報的。上月營收也一樣。

可用日（avail_date）—— 這筆營收何時才真的看得到：

    月營收依規定在次月 10 日前公布。若直接用營收所屬月份對應報酬，
    等於在營收還沒公布時就知道了它 —— 那是 look-ahead bias。

        可用日 = 次月 10 日之後的第一個交易日，再往後推 1 個交易日

    多推一天是刻意的保守緩衝。實測有公司晚到 13 日才公布（假日順延），
    寧可晚一天看到資料，也不要在回測裡拿到當時不存在的資訊 ——
    偏保守只會低估效果，偏樂觀則會製造假的績效。

    已知的小缺口：月報表顯示的是<b>現在</b>的數字，公司事後更正過的會是更正後的值。
    更正很少見、幅度通常很小，先記下來不處理。

原始網頁原封不動 gzip 存在 raw/revenue_mops/YYYYMM.html.gz。已經結束的月份
不會再變；最近兩個月每次都重抓，因為公司還在陸續申報（10 日截止，有人晚交）。
"""
import datetime as dt
import gzip
import re
import sys
import time

import numpy as np
import pandas as pd
import requests

from config import RAW, UA

DIR = RAW / "revenue_mops"
DIR.mkdir(exist_ok=True)
START = (2007, 1)          # 2008 年的年增率與 12 個月新高需要前一年
DELAY = 2.0
BASE = "https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_{}_{}.html"
_CODE = re.compile(r"[0-9A-Z]{4,6}")


def _path(y, m):
    return DIR / "{:04d}{:02d}.html.gz".format(y, m)


def months(end=None):
    """START 到 end（含）的每個 (年, 月)。end 預設為上個月。"""
    t = end or (dt.date.today().replace(day=1) - dt.timedelta(days=1))
    y, m = START
    out = []
    while (y, m) <= (t.year, t.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def fetch_month(y, m, force=False):
    """抓一個月的月報表。回傳 True 表示檔案已就緒（有資料列）。

    網址不帶 _0/_1 後綴的那份是國內＋國外公司合併的完整版；
    _0 只有國內、_1 只有 KY，早年也沒有 _0 版本。"""
    p = _path(y, m)
    if p.exists() and not force:
        return True
    try:
        r = requests.get(BASE.format(y - 1911, m), headers={"User-Agent": UA}, timeout=60)
    except Exception as e:
        print("  {}-{:02d} 連線失敗：{}".format(y, m, e), flush=True)
        return False
    if r.status_code != 200:
        print("  {}-{:02d} HTTP {}".format(y, m, r.status_code), flush=True)
        return False
    # 還沒有公司申報的月份會回一頁空表 —— 不存檔，下次再試
    if not parse(r.content):
        return False
    p.write_bytes(gzip.compress(r.content))
    return True


def update(refresh=2, verbose=True):
    """補齊缺的月份，並重抓最近 refresh 個月。冪等、可中斷續跑。"""
    ms = months()
    recent = set(ms[-refresh:]) if refresh else set()
    todo = [x for x in ms if x in recent or not _path(*x).exists()]
    if verbose:
        print("月營收：{} 個月，要抓 {} 個（其中重抓最近 {} 個）".format(
            len(ms), len(todo), len(recent)), flush=True)
    bad = []
    for i, (y, m) in enumerate(todo):
        if not fetch_month(y, m, force=(y, m) in recent):
            bad.append("{}-{:02d}".format(y, m))
        if i + 1 < len(todo):
            time.sleep(DELAY)
        if verbose and (i + 1) % 24 == 0:
            print("  [{}/{}]".format(i + 1, len(todo)), flush=True)
    have = sum(1 for x in ms if _path(*x).exists())
    if verbose:
        print("完成：{} / {} 個月有資料{}".format(
            have, len(ms), "；沒抓到 " + ", ".join(bad) if bad else ""), flush=True)
    return have, bad


def _num(s):
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return np.nan


def parse(content):
    """月報表 -> [(代號, 當月營收, 上月營收, 去年當月營收)]，金額單位千元。

    同一家公司在舊版頁面會出現在兩個產業底下（數字相同），只留第一筆。
    欄位靠位置取：代號、名稱、當月、上月、去年當月、上月比較%、去年同月%……
    —— 先檢查表頭真的是這個順序，對不上就回傳空的，不猜。"""
    s = content.decode("cp950", "replace") if isinstance(content, bytes) else content
    head = re.sub(r"<[^>]+>|\s", "", s[:s.find("<tr align=right>")] if "<tr align=right>" in s else s)
    if not all(k in head for k in ("當月營收", "上月營收", "去年當月營收")):
        return []
    seen, out = set(), []
    for tr in re.split(r"<tr", s, flags=re.I)[1:]:
        c = [re.sub(r"<[^>]+>", "", x).strip()
             for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.I | re.S)]
        if len(c) < 5 or not _CODE.fullmatch(c[0]) or c[0] in seen:
            continue
        cur = _num(c[2])
        if not np.isfinite(cur):
            continue
        seen.add(c[0])
        out.append((c[0], cur, _num(c[3]), _num(c[4])))
    return out


def load(codes=None):
    """載入所有月報表，回傳長表。

    產出欄位：
        code            股票代號
        avail_date      可用日（依法規期限推算，見模組說明）
        rev_month       營收所屬年月
        revenue         當月營收（元）
        yoy             年增率（與去年同月比，同一基準）
        mom             月增率
        yoy_3m          近 3 個月營收合計的年增率（平滑單月波動）
        sue             優於自己常態的程度
        rev_high12      本月是否為近 12 個月最高
    """
    import ingest
    cal = pd.to_datetime([
        "{}-{}-{}".format(x[:4], x[4:6], x[6:]) for x in ingest.trading_calendar()])
    rows = []
    for p in sorted(DIR.glob("*.html.gz")):
        ym = pd.Timestamp("{}-{}-01".format(p.name[:4], p.name[4:6]))
        try:
            recs = parse(gzip.decompress(p.read_bytes()))
        except Exception as e:
            print("::warning::月營收 {} 讀不出來：{}".format(p.name, e), file=sys.stderr)
            continue
        rows += [(c, ym, cur, prev, ly) for c, cur, prev, ly in recs]
    if not rows:
        return pd.DataFrame()
    d = pd.DataFrame(rows, columns=["code", "rev_month", "cur", "prev", "ly"])
    if codes:
        d = d[d["code"].isin(set(codes))]
    d = d.sort_values(["code", "rev_month"]).reset_index(drop=True)
    d["revenue"] = d["cur"] * 1000
    d["avail_date"] = avail_dates(d["rev_month"], cal)
    d = d[d["avail_date"].notna()].reset_index(drop=True)

    def pct(a, b):
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(b > 0, (a / b - 1) * 100, np.nan)

    d["yoy"] = pct(d["cur"], d["ly"])
    d["mom"] = pct(d["cur"], d["prev"])
    # 3 個月合計：只在連續 3 個月都有申報時才算，缺月不拼湊
    mi = d["rev_month"].dt.year * 12 + d["rev_month"].dt.month
    g = d.groupby("code", sort=False)
    consec = (mi - g["rev_month"].shift(2).pipe(
        lambda s: s.dt.year * 12 + s.dt.month)) == 2
    c3 = g["cur"].transform(lambda s: s.rolling(3).sum())
    l3 = g["ly"].transform(lambda s: s.rolling(3).sum())
    d["yoy_3m"] = np.where(consec, pct(c3, l3), np.nan)

    # SUE（標準化未預期營收）。這是 PEAD（盈餘公布後漂移）的月頻版本。
    #
    # 注意「預期」在這裡的定義：是<b>公司自己過去 12 個月的常態</b>，
    # 不是分析師預估（本系統沒有那個資料）。畫面上一律寫「優於預期」，
    # 但說明必須講清楚基準是什麼，否則會被讀成「優於市場共識」。
    #
    # 跟 yoy 的差別很重要：yoy 是「成長得快不快」，SUE 是「比自己的常態好多少」。
    # 一家連續三年年增 60% 的公司，這個月公布 40%，在年增率排行上仍然名列前茅，
    # 但那其實是<b>低於自己的常態</b>，PEAD 說它接下來會弱。
    #
    #   預期 = 過去 12 個月 yoy 的平均（季節隨機漫步 + 漂移）
    #   SUE  = (本月 yoy − 預期) / 過去 12 個月 yoy 的標準差
    #
    # 除以自己的波動，是為了讓「營收本來就穩定的公司出現 10% 落差」
    # 比「營收本來就亂跳的公司出現 10% 落差」更有意義。
    #
    # 一律 shift(1)：預期只能用本月之前的資料算，否則是偷看。
    gy = d.groupby("code", sort=False)["yoy"]
    exp = gy.transform(lambda s: s.shift(1).rolling(12, min_periods=6).mean())
    sd = gy.transform(lambda s: s.shift(1).rolling(12, min_periods=6).std())
    # 用 np.nan 不是 pd.NA：pd.NA 會把整個 float 欄位轉成 object dtype，
    # 之後 nlargest / 數值運算全部會壞，而 sort_values 剛好還能用 ——
    # 也就是榜單看起來正常，問題在別的地方才爆出來。
    d["sue"] = ((d["yoy"] - exp) / sd).replace([np.inf, -np.inf], np.nan)
    # 本月營收是否為近 12 個月（含本月）最高。短線選股的「月營收強勢」加分項。
    # 只用本月與之前 11 個月，公布時就已知，沒有偷看。
    hi12 = d.groupby("code", sort=False)["revenue"].transform(
        lambda s: s.rolling(12, min_periods=12).max())
    d["rev_high12"] = (d["revenue"] >= hi12).astype(float).where(hi12.notna())
    return d[["code", "avail_date", "rev_month", "revenue", "yoy", "mom",
              "yoy_3m", "sue", "rev_high12"]]


def avail_dates(rev_months, trading_days):
    """把「營收所屬月份」換算成「可用日」。

    次月 10 日 -> 之後第一個交易日 -> 再推 1 個交易日（保守緩衝）。
    """
    td = pd.DatetimeIndex(sorted(pd.unique(trading_days)))
    deadline = (pd.DatetimeIndex(rev_months) + pd.DateOffset(months=1)).map(
        lambda d: d.replace(day=10))
    idx = td.searchsorted(deadline, side="left") + 1     # +1 = 緩衝一個交易日
    idx = idx.clip(0, len(td) - 1)
    out = td[idx]
    # 超出交易日曆範圍的（最新一筆營收可能還沒對應的交易日）標為缺值
    return pd.Series(out).where(pd.Series(deadline) <= td[-1], pd.NaT)


def as_of_panel(rev, dates):
    """把月營收攤成「每個交易日當下可知的最新一筆」。

    這是 point-in-time 的關鍵：某個交易日看得到的，只有 avail_date <= 該日
    的那些營收。用 merge_asof 依 avail_date 對齊，天然不會偷看未來。
    """
    if not len(rev):
        return pd.DataFrame()
    out = []
    # 兩邊的時間解析度必須一致才能 merge_asof。
    #
    # pandas 2.x 之後 datetime64 可能是 ns 也可能是 us，取決於它是怎麼被
    # 建出來的；同一份程式在不同 pandas 版本上會給出不同解析度。本機
    # （pandas 2.3.3）兩邊剛好都是 ns 所以沒事，CI 上較新的版本一邊變成 us，
    # merge_asof 就丟出 "incompatible merge keys ... must be the same type"。
    #
    # 那個例外原本被 server.panel 的 except 吞掉，只留下缺值 —— 表面上
    # 漏斗照跑，實際上 L3 營收層整層失效，名單從 221 檔虛胖成 263 檔。
    # 這裡統一轉成 ns，不要依賴版本預設。
    NS = "datetime64[ns]"
    dates = pd.DatetimeIndex(sorted(pd.unique(dates))).astype(NS)
    rev = rev.copy()
    rev["avail_date"] = rev["avail_date"].astype(NS)
    for code, g in rev.groupby("code", sort=False):
        g = g.sort_values("avail_date")
        left = pd.DataFrame({"date": dates})
        m = pd.merge_asof(
            left, g[["avail_date", "yoy", "yoy_3m", "mom", "sue", "rev_high12",
                     "rev_month"]],
            left_on="date", right_on="avail_date", direction="backward")
        m["code"] = code
        out.append(m)
    r = pd.concat(out, ignore_index=True)
    # 營收太舊（超過 75 天沒更新）視為缺值，避免停止公布的公司留著舊數字
    r.loc[(r["date"] - r["avail_date"]).dt.days > 75,
          ["yoy", "yoy_3m", "mom", "sue", "rev_high12"]] = np.nan
    # 距公布幾天 —— PEAD 是事件驅動的，訊號會隨時間衰減，
    # 公布 3 天的股票跟公布 50 天的不是同一回事，畫面上要分得出來。
    r["days_since"] = (r["date"] - r["avail_date"]).dt.days
    return r[["date", "code", "yoy", "yoy_3m", "mom", "sue", "rev_high12", "days_since",
              "avail_date", "rev_month"]]


if __name__ == "__main__":
    # 補齊缺的月份 + 重抓最近兩個月。改了解析邏輯不必重抓，原始網頁就在 raw/。
    update()
