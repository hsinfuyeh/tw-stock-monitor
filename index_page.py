"""總覽頁 —— 把各報告串成一個站，並在首頁就說清楚系統現況。

刻意放在第一屏的東西：資料涵蓋範圍、驗證結論、已知限制。
理由跟排序頁的成績單一樣 —— 使用者第一眼該看到的是「這套目前能信到什麼程度」，
而不是一份看起來很篤定的名單。
"""
import datetime as dt
import html
from pathlib import Path

import layout
import store
from config import REPORTS, DATASETS, RAW


def _dataset_progress():
    rows = []
    total = 1869
    for ds in DATASETS:
        d = RAW / ds
        n = len(list(d.glob("*.json.gz"))) if d.exists() else 0
        rows.append((ds, n, total, n / total * 100))
    return rows


def _coverage():
    try:
        r = store.q("""SELECT COUNT(*) n, COUNT(DISTINCT date) d,
                       MIN(date) a, MAX(date) b,
                       COUNT(DISTINCT code) c FROM quotes""")
        return dict(rows=int(r.n[0]), days=int(r.d[0]),
                    start=r.a[0], end=r.b[0], codes=int(r.c[0]))
    except Exception:
        return None


def _stock_chips():
    files = sorted(REPORTS.glob("stock_*.html"))
    if not files:
        return '<div class="note">尚未產生個股報告。執行 <code>python run.py stock 2330</code>。</div>'
    chips = []
    for f in files:
        code = f.stem.replace("stock_", "")
        chips.append('<a class="chip" href="{}">{}</a>'.format(f.name, html.escape(code)))
    return '<div class="chips">{}</div>'.format("".join(chips))


def build(findings=None, out=None):
    cov = _coverage()
    asof = "{:%Y-%m-%d}".format(cov["end"]) if cov else "—"

    stats = []
    if cov:
        stats = [("資料涵蓋", "{} 交易日".format(cov["days"])),
                 ("起訖", "{:%Y-%m} ~ {:%Y-%m}".format(cov["start"], cov["end"])),
                 ("證券檔數", "{:,}".format(cov["codes"])),
                 ("面板列數", "{:,}".format(cov["rows"]))]
    sg = "".join('<div class="stat"><div class="k">{}</div><div class="v">{}</div></div>'
                 .format(k, v) for k, v in stats)

    prog = []
    for ds, n, total, pct in _dataset_progress():
        bar = ('<span style="display:inline-block;width:90px;height:7px;border-radius:4px;'
               'background:var(--shade);vertical-align:middle;overflow:hidden">'
               '<span style="display:block;width:{:.0f}%;height:100%;background:var(--accent)">'
               "</span></span>").format(pct)
        prog.append("<tr><td>{}</td><td>{}</td><td>{:,} / {:,}</td><td>{:.0f}%</td></tr>"
                    .format(html.escape(ds), bar, n, total, pct))
    prog_tbl = ('<div class="scroll"><table><thead><tr><th>資料集</th><th>進度</th>'
                "<th>天數</th><th>%</th></tr></thead><tbody>"
                + "".join(prog) + "</tbody></table></div>")

    tiles = [
        ("ranking.html", "上市普通股排序",
         "約 1,080 檔可交易普通股的看漲／看跌名單。等權合成分數，"
         "每列可點進個股 K 線報告。名單上方強制附成績單。"),
        ("ranking_etf.html", "ETF 排序",
         "台股一般型與高股息型 ETF。槓桿／反向／債券／海外已排除，"
         "因子集也與普通股不同（不用價值與法人因子）。"),
    ]
    tile_html = "".join(
        '<a class="tile" href="{}"><h3>{}</h3><p>{}</p></a>'.format(h, t, d)
        for h, t, d in tiles)

    fi = findings or []
    fi_html = "".join("<li>{}</li>".format(x) for x in fi)

    parts = [
        layout.head("台股量化觀測站"),
        layout.nav("index.html", asof),
        '<div class="wrap">',
        "<h1>台股量化觀測站</h1>",
        '<div class="sub">自用工具 ｜ 資料截止 {} ｜ 產生於 {:%Y-%m-%d %H:%M}</div>'.format(
            asof, dt.datetime.now()),

        '<div class="card verdict"><h3>先讀這段</h3><div class="note">'
        "這套系統的價值目前<strong>不在於選股，而在於擋掉沒有根據的東西</strong>。"
        "所有名單都附成績單，未通過驗證時會明講。"
        "合理期待是把勝率從 50% 推到 53%，不是找出會漲的股票。"
        "</div></div>",

        '<div class="grid">{}</div>'.format(sg),

        "<h2>報告入口</h2>",
        '<div class="tiles">{}</div>'.format(tile_html),

        '<h2 id="stocks">個股 K 線報告</h2>',
        _stock_chips(),
        '<div class="note" style="margin-top:8px">'
        "包含 30 日 K 線＋成交量、六種規則、黃色圓點、命中後 1/3/5/10 日報酬與歷史案例統計。"
        "每個報酬同時呈現絕對報酬與相對同日全市場中位數的超額報酬。"
        "產生更多：<code>python run.py stock 2317 2454</code></div>",
    ]
    if fi_html:
        parts += ["<h2>目前已驗證的結論</h2>", '<ul class="note">{}</ul>'.format(fi_html)]

    parts += [
        "<h2>資料回補進度</h2>",
        prog_tbl,
        '<div class="note" style="margin-top:8px">'
        "<code>mi_index</code> 是行情核心，其餘三個提供法人／估值／融資因子。"
        "回補冪等可續跑：<code>python run.py ingest</code></div>",

        "<h2>怎麼用</h2>",
        '<div class="note">'
        "<p><strong>每天更新：</strong><code>python run.py daily</code> —— "
        "抓最近 15 個交易日 → 重建倉儲 → 產生全部報告。約 3–5 分鐘。"
        "台股 14:30 後才有盤後資料，建議晚上跑。</p>"
        "<p><strong>看某一檔：</strong><code>python run.py stock 2330 0050</code></p>"
        "<p><strong>自己查資料：</strong>倉儲是 DuckDB，"
        "<code>python -c \"import store; print(store.q('SELECT ...'))\"</code>。"
        "可用的表：quotes、exrights、inst、valuation、margin。</p>"
        "<p><strong>驗證：</strong><code>python tests.py</code>（36 項資料層）、"
        "<code>python verify.py</code>（29 項統計層）、"
        "<code>python run.py validate</code>（框架自檢＋因子表）。</p>"
        "<p>完整說明見專案根目錄的 <code>USAGE.md</code>。</p></div>",
        "</div>",
        layout.foot(),
    ]
    out = out or (REPORTS / "index.html")
    Path(out).write_text("".join(parts), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(build())
