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

PTT 整站在 Cloudflare 後面：GitHub 的雲端主機直接連（含 RSS）、或從 GitHub 呼叫自己的
Cloudflare Worker 代讀，都是 HTTP 403（2026-09-29、09-30 實測；PTT 的防護看的是最初發起請求的來源）。
從一般使用者的瀏覽器透過 Worker 代讀則是通的，所以 PTT <b>在網頁按「更新」時由瀏覽器讀</b>：
site/assets/social.js 讀最近 3 天的文章、比對股票、算出每天每檔的次數，跟著更新要求送給 Worker
（cloudflare/worker.js），轉交 GitHub，apply_payload() 檢查後併進 snapshots/social/ptt.json
（約 100 KB，workflow 會 commit）。發佈時讀這個檔。沒有人按更新的日子，頁面會寫「PTT 資料停在哪天」。
不靠任何電腦的排程（2026-09-29～30 曾用本機 Windows 排程抓，已移除）。
Threads 走官方 API，雲端抓得到，在 CI 裡查。

這支檔案裡的 PTT 爬蟲（update_ptt、export_ptt）平常不會跑，只留給手動救援用：
匯總檔的歷史掉了的時候，在家用網路跑 python social.py 35 可以一次補回 35 天（網頁只補 3 天）。

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

from config import DATA, RAW, ROOT

DIR = RAW / "social"
SNAP_FILE = ROOT / "snapshots" / "social" / "ptt.json"   # 進版控，雲端讀這個
LOCAL_FILE = DATA / "social_ptt.json"                    # 本機剛匯出、可能還沒 pull 下來的同一份
PTT_DIR = DIR / "ptt"
TH_DIR = DIR / "threads"
TW = dt.timezone(dt.timedelta(hours=8))           # 台北時間；CI 跑在 UTC，不能用本機時區

PTT = "https://www.ptt.cc"       # 只有手動回補（python social.py N）會直接連；雲端連不到
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
LAST_ERR = [None]      # 最近一次失敗的原因（狀態碼或例外），抓不到時寫進 log 與頁面


def _get(url, tries=3):
    for i in range(tries):
        try:
            r = _s.get(url, timeout=20)
            if r.status_code == 200:
                return r.text
            LAST_ERR[0] = "HTTP {}".format(r.status_code)
            if r.status_code == 404:
                return None
        except requests.RequestException as e:
            LAST_ERR[0] = type(e).__name__ + ": " + str(e)[:120]
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
            if pages == 1:
                log("::warning::PTT 股票板列表抓不到（{}）".format(LAST_ERR[0] or "頁面上沒有文章"))
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


def update(data_date=None, threads_n=400):
    """CI 每次發佈前呼叫：只查 Threads（有金鑰才查，而且只在晚上 21 點後那次，每天額度有限）。
    PTT 不在這裡抓 —— 雲端連不到，由網頁按「更新」時送進來（apply_payload）。不丟例外。"""
    if not threads_enabled():
        return
    now = dt.datetime.now(TW)
    if now.hour < 21:
        log("Threads：晚上 21 點後那次排程才查（每天額度有限）")
        return
    try:
        names = _names()
        dc = daily_counts(load_ptt())
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


def prune_ptt(days=60):
    """60 天前的文章用不到了（基準只看前 20 個交易日），刪掉。"""
    old = time.time() - days * 86400
    for f in (PTT_DIR.glob("*.json.gz") if PTT_DIR.exists() else []):
        try:
            if int(f.name.split(".")[1]) < old:
                f.unlink()
        except (IndexError, ValueError, OSError):
            pass


def export_ptt(names=None, prev=None, to_repo=False):
    """把 raw/social/ptt/ 的文章匯總成 {date: {code: [文章數, 推文數]}}，寫到 LOCAL_FILE 並回傳。

    prev：上一份匯總（load_ptt()），原文沒涵蓋到的舊日子沿用它。
    to_repo：同時寫進版控的 snapshots/social/ptt.json（手動回補用，之後自己 commit）。
    比對（Matcher）在這一步做，所以改排除清單之後，原文還在的日子下一次匯出會照新規則重算。"""
    names = names or _names()
    m = Matcher(names)
    days, arts = {}, {}
    for p in PTT_DIR.glob("*.json.gz"):
        try:
            a = _read(p)
        except Exception:
            continue
        arts[a["date"]] = arts.get(a["date"], 0) + 1
        for c in m.find(a["title"] + "\n" + a["body"]):
            days.setdefault(a["date"], {}).setdefault(c, [0, 0])[0] += 1
        for day, txt in a["pushes"]:
            for c in m.find(txt):
                days.setdefault(day, {}).setdefault(c, [0, 0])[1] += 1
    # 手上的原文不一定涵蓋全部歷史（只回補了幾天、或舊的原文已經刪掉），
    # 更早的日子沿用上一份匯總。「最舊那篇文章的日期」之後的日子原文是完整的，以這次重算為準；
    # 那一天（只抓到半天）與更早的，跟上一份逐格取大的 —— 提及次數只會隨推文增加，不會變少。
    if prev:
        oldest = min(arts) if arts else "9999"
        keep_from = (dt.datetime.now(TW).date() - dt.timedelta(days=60)).isoformat()
        for d, v in (prev.get("days") or {}).items():
            if d > oldest or d < keep_from:
                continue
            cur = days.setdefault(d, {})
            for c, (n_post, n_push) in v.items():
                a = cur.setdefault(c, [0, 0])
                a[0], a[1] = max(a[0], n_post), max(a[1], n_push)
        for d, n in (prev.get("articles") or {}).items():
            if keep_from <= d <= oldest:
                arts[d] = max(arts.get(d, 0), n)
    obj = dict(updated_at=dt.datetime.now(TW).isoformat(timespec="seconds"),
               articles=dict(sorted(arts.items())),
               days={d: dict(sorted(v.items())) for d, v in sorted(days.items())})
    txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    for f in ([LOCAL_FILE, SNAP_FILE] if to_repo else [LOCAL_FILE]):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(txt, encoding="utf-8")
    return obj


PAYLOAD_DAYS = 5        # 網頁送來的聲量只收最近幾天的（它每次只讀最近 3 天的文章）
PAYLOAD_CAP = 3000      # 一檔一天的上限。實際最高是幾百；超過的當成壞資料丟掉


def apply_payload(text, names=None):
    """網頁按「更新」時，瀏覽器算好送來的 PTT 聲量（見 site/assets/social.js、cloudflare/worker.js）。

    那個入口是公開的、不需要密碼，所以送進來的東西一律當成不可信：
    只收最近 PAYLOAD_DAYS 天、只收真的存在的代號、數字要是 0～PAYLOAD_CAP 的整數，
    其他一概丟掉。通過的跟現有匯總逐格取大的（提及次數只會隨推文增加），寫回
    snapshots/social/ptt.json（workflow 會 commit）。回傳收下幾格；格式不對回 0、不丟例外。

    這擋不住有心人送「看起來合理」的假數字，影響只限社群聲量兩張榜（只顯示、不計分）。"""
    try:
        p = json.loads(text)
        days_in = p.get("days") or {}
        arts_in = p.get("articles") or {}
        assert isinstance(days_in, dict) and isinstance(arts_in, dict)
    except Exception as e:
        log("::warning::社群聲量：網頁送來的內容格式不對，忽略（{}）".format(str(e)[:80]))
        return 0
    names = names or _names()
    today = dt.datetime.now(TW).date()
    ok_days = {(today - dt.timedelta(days=i)).isoformat() for i in range(PAYLOAD_DAYS)}

    def num(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= PAYLOAD_CAP else None

    cur = load_ptt() or dict(days={}, articles={})
    days, arts, n = cur.get("days") or {}, cur.get("articles") or {}, 0
    for d, v in days_in.items():
        if d not in ok_days or not isinstance(v, dict):
            continue
        for c, pair in v.items():
            if c not in names or not (isinstance(pair, list) and len(pair) == 2):
                continue
            a, b = num(pair[0]), num(pair[1])
            if a is None or b is None or a + b == 0:
                continue
            old = days.setdefault(d, {}).get(c, [0, 0])
            days[d][c] = [max(old[0], a), max(old[1], b)]
            n += 1
    for d, v in arts_in.items():
        if d in ok_days and num(v) is not None and v <= 500:        # 股票板一天 35–50 篇
            arts[d] = max(arts.get(d, 0), v)
    if not n:
        log("社群聲量：網頁送來的內容沒有可用的數字，忽略")
        return 0
    obj = dict(updated_at=dt.datetime.now(TW).isoformat(timespec="seconds"),
               articles=dict(sorted(arts.items())),
               days={d: dict(sorted(v.items())) for d, v in sorted(days.items())})
    txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    for f in (LOCAL_FILE, SNAP_FILE):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(txt, encoding="utf-8")
    log("社群聲量：收下網頁送來的 {} 格（{}）".format(n, "、".join(sorted(d for d in days_in if d in ok_days))))
    return n


# 比對規則給網頁用（publish 寫成 data/social_rules.json）。網頁的比對（social.js）是照 Matcher
# 重寫的一份，所以附上這幾句的正確答案：網頁開始抓之前先自己對一次，對不上就不送，
# 免得兩邊規則漂移之後，悄悄送進跟 Python 算法不一樣的數字。
RULE_CASES = [
    "2330 台積電 多", "東南亞市場", "南亞科漲停", "長榮航大漲", "世界很大", "世界先進法說",
    "群聯2300點", "2026/09/28 盤後", "爆出大量", "2027 年產能", "2027大成鋼 漲停", "大成功",
    "我的海力士", "買了1000張", "聯發科跟鴻海", "中華車銷量", "川寶又發文", "1303 南亞 36.5",
]


def rules(names=None):
    names = names or _names()
    m = Matcher(names)
    return dict(names=names, ambiguous=sorted(AMBIGUOUS), aliases=ALIASES, strip=STRIP,
                cases=[dict(text=t, codes=sorted(m.find(t))) for t in RULE_CASES])


def load_ptt():
    """PTT 匯總檔：版控裡的（雲端）與本機剛匯出的，取比較新的那份。都沒有回 None。"""
    best = None
    for f in (SNAP_FILE, LOCAL_FILE):
        try:
            o = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if best is None or o.get("updated_at", "") > best.get("updated_at", ""):
            best = o
    return best


def daily_counts(ptt=None):
    """每檔股票每個日曆日的提及次數：DataFrame(date, code, src, posts, pushes)。"""
    rows = []
    for day, v in ((ptt or {}).get("days") or {}).items():
        for c, (n_post, n_push) in v.items():
            rows.append((day, c, "ptt", n_post, n_push))
    for p in (TH_DIR.glob("*.json.gz") if TH_DIR.exists() else []):
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
    ptt = load_ptt()
    dc = daily_counts(ptt)
    cal = [d for d in cal if d <= data_date]
    win = windows(cal, BASELINE)
    today_days = win.get(data_date, [data_date])
    ptt_at = (ptt or {}).get("updated_at")
    info = dict(window=[today_days[0], today_days[-1]], ptt_updated=ptt_at,
                # 匯總檔在這個交易日結束前就停了（電腦沒開）：數字只算到那個時間
                ptt_stale=bool(ptt_at) and ptt_at[:10] < today_days[-1],
                sources=[dict(key="ptt", name="PTT 股票板", status="missing" if ptt is None else "ok",
                              articles=sum((ptt or {}).get("articles", {}).get(d, 0) for d in today_days))])
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
    return cur.reset_index(), info


if __name__ == "__main__":
    # 手動救援：匯總檔的歷史掉了的時候，在家用網路（雲端連不到 PTT）跑 python social.py 35，
    # 會抓 35 天、併進 snapshots/social/ptt.json；之後自己 git add / commit / push 那個檔。
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    prev = load_ptt()
    update_ptt(days=days, budget=100000)
    export_ptt(prev=prev, to_repo=True)
