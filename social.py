"""社群聲量：每檔股票在社群上被討論了多少次（只顯示、不計分）。

「其他排行」裡的兩張榜：聲量最高、聲量暴增。跟上櫃、主動 ETF 一樣是附加資訊 ——
不進選股、研究與前瞻實測，抓不到也不擋住發佈。

來源與限制（2026-09-29 實測）：

    PTT 股票板   公開網頁，標題與內文會直接寫股票名稱或代號，推文有時間戳。穩定。
    Threads      只能走 Meta 官方 API（keyword_search），要 threads_keyword_search 權限
                 通過審核才搜得到公開貼文，沒通過只會搜到自己的貼文。
                 環境變數 THREADS_TOKEN 有設才會抓；沒設就標「未啟用」。
    Dcard、Mobile01   Cloudflare 直接擋（403），雲端排程不可能通過，不做。
    CMoney 爆料同學會 公開網頁裡沒有個股文章，文章是用登入憑證打私有 API 拿的，不做。

原始資料存在 raw/social/：每篇 PTT 文章一個檔（標題、內文、推文與時間），
股票比對在匯總時才做 —— 改排除清單不必重抓。

「聲量」＝ 一個交易日的窗口內，提到這檔股票的文章數 + 推文數 + Threads 貼文數。
窗口是「上一個交易日之後 ~ 這個交易日」的日曆日，所以連假期間的討論會算進開市那天。
「暴增」＝ 這一個窗口的聲量 ÷ 前 20 個交易日窗口的平均聲量。

研究上（Barber & Odean 2008；Da, Engelberg & Gao 2011）散戶關注突然升高的股票，
常常短線過熱、之後反轉 —— 這兩張榜是「今天大家在吵什麼」，不是買進名單。
"""
import datetime as dt
import gzip
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

from config import RAW

DIR = RAW / "social"
PTT_DIR = DIR / "ptt"
TH_DIR = DIR / "threads"
TW = dt.timezone(dt.timedelta(hours=8))           # 台北時間；CI 跑在 UTC，不能用本機時區

PTT = "https://www.ptt.cc"
UA = "Mozilla/5.0 (compatible; personal-research/1.0)"
_s = requests.Session()
_s.headers.update({"User-Agent": UA})
_s.cookies.set("over18", "1", domain="www.ptt.cc")

BASELINE = 20          # 暴增的基準：前幾個交易日窗口的平均
MIN_MENTIONS = 5       # 暴增榜的門檻：聲量太小的「10 倍」只是 1 次變 10 次，沒有意義

# 兩個字的股票名稱裡，同時是日常用語的。這些只認代號，不認名稱 ——
# 否則「世界盃」會算成世界先進、「全家人」算成全家、「中華民國」算成中華車。
# 名單是 2026-09-29 逐一看過全部 1,240 個兩字名稱，再對照 PTT 股票板實際比對結果挑出來的。
AMBIGUOUS = set("""
三星 世界 中天 中華 中視 互動 京城 傳奇 信義 光明 光環 光譜 全台 全國 全家 全新 典範 冠軍
創意 卓越 南港 台南 台端 合一 國產 地球 大同 大樹 大洋 大甲 大華 大量 大宇 大塚 奇偶 安可
安心 宏觀 尖點 巨大 帝寶 幸福 建國 得力 恆大 惠普 新興 新華 星雲 春雨 有益 東森 東洋 根基
樂意 正道 正文 泰山 海灣 無敵 王座 神準 神盾 秋雨 立誠 精確 精華 精英 精誠 統一 綠意 綠電
聯合 華夏 藍天 資通 進階 遠見 長虹 雙喜 霹靂 青雲 順天 鳳凰 至上 辣椒 橘子 櫻花 花王 川寶
數字 上品 加高 滿心 大成
""".split())

# 被上面擋掉的股票，常用的完整稱呼另外補回來（這些不會跟日常用語撞）。
ALIASES = {
    "世界先進": "5347", "中華車": "2204", "中華汽車": "2204", "統一企業": "1216", "巨大機械": "9921",
    "全家便利": "5903", "大同公司": "2371", "神盾股份": "6462", "東森國際": "2614", "精英電腦": "2331",
    "新興航運": "2605", "泰山企業": "1218", "王座國際": "2751",
}

# 比對前先挖掉的詞：裡面剛好包含某檔股票的名稱，但講的不是它。
# 東南亞 ⊃ 南亞、海力士（SK 海力士）⊃ 力士、聯合報 ⊃ 聯合。
STRIP = ["東南亞", "海力士", "聯合報"]


def log(msg):
    print(msg, flush=True)


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _read(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------- PTT
def _get(url, tries=3):
    for i in range(tries):
        try:
            r = _s.get(url, timeout=20)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    return None


_ENTRY = re.compile(r'<div class="title">\s*<a href="(/bbs/Stock/(M\.(\d+)\.A\.[0-9A-F]+)\.html)">(.*?)</a>',
                    re.S)
_PREV = re.compile(r'<a class="btn wide" href="(/bbs/Stock/index\d+\.html)">&lsaquo; 上頁</a>')


def _index_page(url):
    """一頁文章列表：[(id, 發文時間 epoch, 標題)]、上一頁網址。置底文（公告）不算。"""
    html = _get(url)
    if html is None:
        return [], None
    body = html.split('<div class="r-list-sep"></div>')[0]     # 分隔線以下是置底公告
    items = [(m.group(2), int(m.group(3)), _unescape(m.group(4))) for m in _ENTRY.finditer(body)]
    prev = _PREV.search(html)
    return items, (PTT + prev.group(1)) if prev else None


def _unescape(s):
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'").strip())


_TAG = re.compile(r"<[^>]+>")
_PUSH = re.compile(r'<div class="push">.*?<span class="[^"]*push-content">(.*?)</span>\s*'
                   r'<span class="push-ipdatetime">(.*?)</span>', re.S)


def _article(aid, epoch):
    """一篇文章：標題、內文（不含推文）、推文 [(日期, 內容)]。"""
    html = _get("{}/bbs/Stock/{}.html".format(PTT, aid))
    if html is None:
        return None
    m = re.search(r'<span class="article-meta-tag">標題</span><span class="article-meta-value">(.*?)</span>', html)
    title = _unescape(m.group(1)) if m else ""
    main = html.split('<div id="main-content"', 1)[-1].split(">", 1)[-1]
    main = main.split('<div class="push">', 1)[0]
    # 作者／看板／標題／時間那幾行：標題已經另外算了，留著會重複計算
    main = re.sub(r'<div class="article-metaline(?:-right)?">.*?</div>', " ", main, flags=re.S)
    body = _unescape(_TAG.sub(" ", main))
    body = body.split("※ 發信站")[0][:20000]
    posted = dt.datetime.fromtimestamp(epoch, TW)
    pushes = []
    for c, ipdt in _PUSH.findall(html):
        dm = re.search(r"(\d{2})/(\d{2})", ipdt)
        if not dm:
            continue
        mo, d = int(dm.group(1)), int(dm.group(2))
        y = posted.year + (1 if mo < posted.month - 6 else 0)   # 12 月的文、1 月的推文
        try:
            day = dt.date(y, mo, d).isoformat()
        except ValueError:
            continue
        pushes.append([day, _unescape(_TAG.sub("", c)).lstrip(": ").strip()[:200]])
    return dict(id=aid, epoch=epoch, date=posted.date().isoformat(), title=title,
                body=body, pushes=pushes,
                fetched=int(time.time()))


def update_ptt(days=3, budget=600, delay=0.4):
    """抓最近 days 天的 PTT 股票板文章。回傳這次抓了幾篇。

    發文 2 天內的文章每小時最多重抓一次（推文還在長），更舊的抓過就不再抓。
    budget 是這次最多抓幾篇文章，CI 的時間有限；沒抓完的下次繼續。"""
    PTT_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    cutoff = now - days * 86400
    url, todo, pages = PTT + "/bbs/Stock/index.html", [], 0
    while url and pages < days * 15 + 10:
        items, url = _index_page(url)
        pages += 1
        if not items:
            break
        for aid, ep, _t in items:
            if ep < cutoff:
                continue
            p = PTT_DIR / "{}.json.gz".format(aid)
            if p.exists():
                try:
                    old = _read(p)
                    if now - ep > 2 * 86400 or now - old.get("fetched", 0) < 3600:
                        continue
                except Exception:
                    pass
            todo.append((aid, ep))
        if min(ep for _a, ep, _t in items) < cutoff:
            break
        time.sleep(delay)
    todo.sort(key=lambda x: -x[1])                      # 新的先抓
    n = 0
    for aid, ep in todo[:budget]:
        a = _article(aid, ep)
        if a is not None:
            _write(PTT_DIR / "{}.json.gz".format(aid), a)
            n += 1
        time.sleep(delay)
    log("PTT 股票板：列表 {} 頁、待抓 {} 篇、這次抓了 {} 篇".format(pages, len(todo), n))
    return n


# --------------------------------------------------------------------- Threads
TH_API = "https://graph.threads.net/v1.0/keyword_search"


def threads_enabled():
    return bool(os.environ.get("THREADS_TOKEN"))


def update_threads(names, day, delay=0.3):
    """用官方 keyword_search 查每檔股票在 day（台北日期，'YYYY-MM-DD'）當天的貼文數。

    names: {code: name}，要查哪些股票（呼叫端挑聲量前幾百名，見 update）。一檔一次查詢，
    每檔每天只查一次 —— 額度是 2,200 次 / 24 小時（沒有結果的查詢不算），
    一天 5 次排程都重查會超過。所以只在晚上那次（21 點後）查，涵蓋當天 00:00 到查詢當下。
    limit 100，超過 100 篇記成 100（榜首本來就只看相對高低）。
    沒有 THREADS_TOKEN 就什麼都不做。回傳狀態字串。"""
    tok = os.environ.get("THREADS_TOKEN")
    if not tok:
        return "disabled"
    p = TH_DIR / "{}.json.gz".format(day)
    have = _read(p) if p.exists() else {}
    d0 = dt.datetime.fromisoformat(day).replace(tzinfo=TW)
    since = int(d0.timestamp())
    until = int(min(time.time() - 60, (d0 + dt.timedelta(days=1)).timestamp()))
    n, status = 0, "ok"
    for code, name in names.items():
        if code in have:
            continue
        q = name if name not in AMBIGUOUS else code
        try:
            r = requests.get(TH_API, params=dict(q=q, search_type="RECENT", since=since, until=until,
                                                 limit=100, fields="id,timestamp", access_token=tok),
                             timeout=20)
        except requests.RequestException as e:
            status = "error: {}".format(e)[:120]
            break
        if r.status_code in (401, 403):
            status = "auth: {}".format(r.text[:160])        # 金鑰過期（長效金鑰 60 天）或權限沒過審
            break
        if r.status_code == 429:
            status = "rate-limited"
            break
        if r.status_code != 200:
            continue
        have[code] = dict(n=len(r.json().get("data", [])))
        n += 1
        time.sleep(delay)
    if have:
        _write(p, have)
    if status != "ok":
        log("::warning::Threads 查詢中止：{}".format(status))
    log("Threads：{} 查了 {} 檔（狀態 {}）".format(day, n, status))
    return status


def update(data_date=None, ptt_budget=400, threads_n=400):
    """CI 每次發佈前呼叫。PTT 抓最近幾天（沒有歷史就一路補到 35 天前，每次 ptt_budget 篇，
    新的先抓；一篇約 0.7 秒。股票板一天約 35 篇，35 天約 1,200 篇，三次排程就補齊）；晚上 21 點後那次再查 Threads。不丟例外。"""
    # 60 天前的文章用不到了（基準只看前 20 個交易日），刪掉，CI 快取才不會越長越大
    old = time.time() - 60 * 86400
    for f in (PTT_DIR.glob("*.json.gz") if PTT_DIR.exists() else []):
        try:
            if int(f.name.split(".")[1]) < old:
                f.unlink()
        except (IndexError, ValueError, OSError):
            pass
    try:
        eps = [int(f.name.split(".")[1]) for f in PTT_DIR.glob("*.json.gz")] if PTT_DIR.exists() else []
        # 最舊的一篇不到 30 天前，代表基準還沒補齊：往回抓 35 天（新的先抓，每次 ptt_budget 篇）
        full = bool(eps) and min(eps) < time.time() - 30 * 86400
        update_ptt(days=3 if full else 35, budget=ptt_budget)
    except Exception as e:
        log("::warning::PTT 抓取失敗：{}".format(e))
    if not threads_enabled():
        return
    now = dt.datetime.now(TW)
    if now.hour < 21:
        log("Threads：晚上 21 點後那次排程才查（每天額度有限）")
        return
    try:
        names = _names()
        dc = daily_counts(names)
        top = (dc.groupby("code")[["posts", "pushes"]].sum().sum(axis=1)
               .sort_values(ascending=False).head(threads_n).index) if len(dc) else list(names)[:threads_n]
        update_threads({c: names[c] for c in top if c in names}, (data_date or now.date().isoformat()))
    except Exception as e:
        log("::warning::Threads 更新失敗：{}".format(e))


# --------------------------------------------------------------------- 比對與匯總
class Matcher:
    """在一段文字裡找出提到了哪些股票。

    代號：前後不能是數字、「/」「-」「.」「:」（排除日期、價格、時間），後面不能接年、點、元、張這類單位。
    名稱：長的先比對、比對到的字就挖掉 —— 「長榮航」不會再被算成「長榮」。
    兩字名稱在 AMBIGUOUS 裡的只認代號；長得像年份的代號（1990–2035）要同時出現名稱才算。"""

    def __init__(self, names):
        self.names = names
        # 後面接單位的是數字不是代號：「2300點」「1000元」「3000張」
        self.code_re = re.compile(r"(?<![\d/\-.:$])(" + "|".join(sorted(names, key=len, reverse=True))
                                  + r")(?![\d/\-.:年月日點元塊張萬億%])")
        by_name = {}
        for c, n in names.items():
            n = n.strip()
            if len(n) >= 2 and n not in AMBIGUOUS:
                by_name.setdefault(n, c)
        for n, c in ALIASES.items():
            if c in names:
                by_name[n] = c
        self.by_name = by_name
        self.name_re = re.compile("|".join(re.escape(n) for n in sorted(by_name, key=len, reverse=True)))

    def find(self, text):
        for w in STRIP:
            text = text.replace(w, " ")
        by_name = {self.by_name[m] for m in self.name_re.findall(text)}
        # 長得像年份的代號（2022、2027、2030…）大多是在講年份（「2028 年產能」「2030 再說」），
        # 同一段文字裡也出現名稱才算 —— 「2027大成鋼」「2027 大成鋼」照樣算得到
        codes = {c for c in self.code_re.findall(text)
                 if not (1990 <= int(c) <= 2035 and c not in by_name
                         and self.names.get(c, "") not in text)}
        return codes | by_name


def _names():
    """可比對的股票：上市普通股 + 上櫃（ETF 不算 —— 0050、高股息會永遠霸榜）。"""
    import server
    u = server.universe()
    u = u[u["cat"].isin(["common", "otc"])]
    return dict(zip(u["code"].astype(str), u["name"].astype(str)))


def daily_counts(names=None):
    """每檔股票每個日曆日的提及次數：DataFrame(date, code, src, posts, pushes)。"""
    names = names or _names()
    m = Matcher(names)
    rows = []
    for p in PTT_DIR.glob("*.json.gz"):
        try:
            a = _read(p)
        except Exception:
            continue
        for c in m.find(a["title"] + "\n" + a["body"]):
            rows.append((a["date"], c, "ptt", 1, 0))
        for day, txt in a["pushes"]:
            for c in m.find(txt):
                rows.append((day, c, "ptt", 0, 1))
    for p in TH_DIR.glob("*.json.gz"):
        day = p.name[:10]
        for c, v in _read(p).items():
            if v.get("n"):
                rows.append((day, c, "threads", v["n"], 0))
    df = pd.DataFrame(rows, columns=["date", "code", "src", "posts", "pushes"])
    return df.groupby(["date", "code", "src"], as_index=False).sum()


def windows(cal, n):
    """最近 n+1 個交易日各自的日曆窗口：{交易日: [日曆日...]}（上一個交易日之後 ~ 這一天）。"""
    cal = sorted(cal)[-(n + 2):]
    out = {}
    for prev, cur in zip(cal, cal[1:]):
        a = dt.date.fromisoformat(prev) + dt.timedelta(days=1)
        b = dt.date.fromisoformat(cur)
        out[cur] = [(a + dt.timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]
    return out


def ranking(data_date, cal):
    """data_date（'YYYY-MM-DD'）的聲量排行。cal：交易日清單（'YYYY-MM-DD'）。

    回傳 (DataFrame, info)。DataFrame 欄位：code, ptt_posts, ptt_pushes, threads, total, base, ratio。
    info 說明涵蓋範圍（前端顯示用）。"""
    names = _names()
    dc = daily_counts(names)
    cal = [d for d in cal if d <= data_date]
    win = windows(cal, BASELINE)
    today_days = win.get(data_date, [data_date])
    info = dict(window=[today_days[0], today_days[-1]], sources=[])
    if not len(dc):
        return pd.DataFrame(), info

    def agg(days):
        x = dc[dc["date"].isin(days)]
        p = x[x["src"] == "ptt"].groupby("code")[["posts", "pushes"]].sum()
        t = x[x["src"] == "threads"].groupby("code")["posts"].sum()
        out = pd.DataFrame(index=sorted(set(p.index) | set(t.index)))
        out["ptt_posts"] = p["posts"].reindex(out.index).fillna(0).astype(int)
        out["ptt_pushes"] = p["pushes"].reindex(out.index).fillna(0).astype(int)
        out["threads"] = t.reindex(out.index).fillna(0).astype(int)
        out["total"] = out.sum(axis=1)
        return out

    cur = agg(today_days)
    # 基準：前 BASELINE 個交易日窗口，只算「PTT 資料有涵蓋到的」窗口 —— 剛開始收集的前幾週
    # 沒有歷史，拿 0 當基準會讓每一檔都是「暴增」。
    have_days = set(dc.loc[dc["src"] == "ptt", "date"])
    prior = [d for d in cal[:-1] if d in win and d != data_date][-BASELINE:]
    prior = [d for d in prior if any(x in have_days for x in win[d])]
    tot = pd.DataFrame({d: agg(win[d])["total"] for d in prior}).fillna(0) if prior else pd.DataFrame()
    cur["base"] = tot.mean(axis=1).reindex(cur.index).fillna(0) if len(tot.columns) else float("nan")
    cur["ratio"] = (cur["total"] + 1) / (cur["base"] + 1)       # +1：從 0 到 3 不該是無限大倍
    cur.index.name = "code"
    info["base_windows"] = len(prior)
    # 這段期間股票板一共幾篇文章（檔名 M.<發文時間>.A.xxx 就有時間，不必打開檔案）
    n_art = sum(1 for f in PTT_DIR.glob("*.json.gz")
                if dt.datetime.fromtimestamp(int(f.name.split(".")[1]), TW).date().isoformat() in today_days)
    info["sources"] = [dict(key="ptt", name="PTT 股票板", status="ok" if len(cur) else "empty",
                            articles=n_art)]
    return cur.reset_index(), info


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    update_ptt(days=days, budget=100000)
