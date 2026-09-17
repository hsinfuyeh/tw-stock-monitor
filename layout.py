"""共用版面層 —— CSS、導覽列、頁首頁尾。

抽出來的原因：report.py 與 stock_report.py 原本各自維護一份幾乎相同的 CSS，
改一邊忘了另一邊就會兩份報告長得不一樣。所有頁面共用同一份樣式與導覽。

檔名策略：一律用固定檔名（index.html / ranking.html / ranking_etf.html /
stock_XXXX.html），不帶日期。日期寫在頁面內容裡。這樣任何頁面互相連結都不會斷，
而且倉儲隨時能重新產生任一天的報告。
"""
import datetime as dt
import html

CSS = """
:root{--bg:#fbfaf9;--fg:#23201d;--mut:#6b6560;--line:#e3ded8;--card:#fff;
--up:#c8332f;--dn:#1a7a4c;--warn:#b8860b;--accent:#2f5d8a;--shade:#f4f1ed;
--hi:#e8b400}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#16150f;--fg:#ebe6df;--mut:#9a938b;--line:#332f28;--card:#1e1c16;
--up:#e8615c;--dn:#4fc08a;--warn:#d9a93a;--accent:#7aa9d6;--shade:#242119;
--hi:#d9a93a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI","Noto Sans TC","Microsoft JhengHei",sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px 80px}
.nav{position:sticky;top:0;z-index:9;background:var(--bg);
border-bottom:1px solid var(--line);margin-bottom:26px}
.nav .inner{max-width:1180px;margin:0 auto;padding:11px 20px;display:flex;
gap:4px;align-items:center;flex-wrap:wrap}
.nav a{padding:5px 11px;border-radius:6px;color:var(--mut);font-size:13px;
font-weight:500;text-decoration:none;white-space:nowrap}
.nav a:hover{background:var(--shade);color:var(--fg);text-decoration:none}
.nav a.on{background:var(--shade);color:var(--fg)}
.nav .sp{flex:1}
.nav .meta{font-size:11.5px;color:var(--mut)}
h1{font-size:22px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:14px;margin:32px 0 12px;padding-bottom:7px;border-bottom:1px solid var(--line);
letter-spacing:.06em;color:var(--mut);font-weight:600}
.sub{color:var(--mut);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:15px 17px}
.verdict{border-left:4px solid var(--warn);margin-bottom:24px}
.verdict.ok{border-left-color:var(--dn)}
.verdict.bad{border-left-color:var(--up)}
.verdict h3{margin:0 0 9px;font-size:15px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(136px,1fr));gap:12px;margin:14px 0 24px}
.stat{background:var(--shade);border-radius:7px;padding:10px 12px}
.stat .k{font-size:11px;color:var(--mut)}
.stat .v{font-size:18px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:right;font-weight:600;color:var(--mut);font-size:11px;letter-spacing:.04em;
padding:7px 9px;border-bottom:1px solid var(--line);white-space:nowrap}
th:nth-child(-n+2),td:nth-child(-n+2){text-align:left}
td{padding:7px 9px;border-bottom:1px solid var(--line);
font-variant-numeric:tabular-nums;white-space:nowrap;text-align:right}
tbody tr:hover{background:var(--shade)}
.code{font-weight:600}
.code a{color:var(--fg)}
.code a:hover{color:var(--accent)}
.nm{color:var(--mut);font-size:12px}
.up,.pos{color:var(--up)}
.dn,.neg{color:var(--dn)}
.mut{color:var(--mut)}
.ex{display:block;font-size:10.5px;color:var(--mut);font-weight:400}
.bar{display:inline-block;height:8px;border-radius:2px;vertical-align:middle}
.tag{display:inline-block;font-size:10px;padding:1px 6px;border-radius:3px;
background:var(--shade);color:var(--mut);margin-left:4px}
.tag.w{background:rgba(184,134,11,.18);color:var(--warn)}
.note{color:var(--mut);font-size:12.5px;line-height:1.75}
.note li{margin-bottom:5px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--hi);
vertical-align:middle;margin:0 3px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}
.tile{display:block;background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;color:var(--fg);text-decoration:none}
.tile:hover{border-color:var(--accent);text-decoration:none}
.tile h3{margin:0 0 6px;font-size:15px}
.tile p{margin:0;font-size:12.5px;color:var(--mut);line-height:1.65}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.chip{display:inline-block;padding:3px 10px;border-radius:14px;background:var(--shade);
font-size:12px;color:var(--fg);text-decoration:none;font-variant-numeric:tabular-nums}
.chip:hover{background:var(--accent);color:#fff;text-decoration:none}
"""

PAGES = [("index.html", "總覽"),
         ("ranking.html", "上市普通股排序"),
         ("ranking_etf.html", "ETF 排序"),
         ("index.html#stocks", "個股報告")]


def nav(current, asof=None, extra=""):
    """頂部導覽。current 是目前頁面的檔名；個股頁傳 'stock'。"""
    items = []
    for href, label in PAGES:
        if href.endswith("#stocks"):
            on = "on" if current == "stock" else ""
            if current == "stock" and extra:
                label = extra
        else:
            on = "on" if href == current else ""
        items.append('<a href="{}" class="{}">{}</a>'.format(href, on, html.escape(label)))
    meta = ""
    if asof:
        meta = '<span class="meta">資料截止 {} ｜ 產生於 {:%m-%d %H:%M}</span>'.format(
            asof, dt.datetime.now())
    return ('<div class="nav"><div class="inner">' + "".join(items)
            + '<span class="sp"></span>' + meta + "</div></div>")


def head(title):
    return ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>{}</title><style>{}</style></head><body>".format(
                html.escape(title), CSS))


def foot():
    return ('<div class="wrap"><div class="note" style="margin-top:40px;'
            'padding-top:16px;border-top:1px solid var(--line)">'
            "資料來源：臺灣證券交易所（MI_INDEX／T86／BWIBBU_d／MI_MARGN／TWT49U）。"
            "本工具僅整理已發生的歷史資料與統計，不預測未來，不構成投資建議。"
            "自用；若對外提供或收費，在台灣屬證券投資顧問業務，需要執照。"
            "</div></div></body></html>")


def stock_link(code, text=None):
    """個股報告連結。

    報告檔不存在時回傳純文字而非連結 —— 排序名單的長度與已產生報告數
    未必一致（例如只補產了部分個股），直接輸出連結會產生死連結。
    """
    from config import REPORTS
    code = str(code)
    label = html.escape(str(text if text is not None else code))
    if (REPORTS / "stock_{}.html".format(code)).exists():
        return '<a href="stock_{}.html">{}</a>'.format(html.escape(code), label)
    return label
