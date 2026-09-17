"""L3 儲存層 — 原始檔 -> DuckDB。

重建策略：從 raw/ 全量重建。原始檔不可變，所以重建永遠可重現；
解析邏輯改了直接重跑即可，不必重抓。2.6M 列在 DuckDB 是秒級。
"""
import sys, time
import pandas as pd
import duckdb
import ingest, parse
from config import DB, DATASETS

TABLES = dict(mi_index="quotes", t86="inst", bwibbu="valuation", margin="margin")


def build(datasets=None, verbose=True):
    datasets = datasets or list(DATASETS)
    days = ingest.trading_calendar()
    con = duckdb.connect(str(DB))
    for ds in datasets:
        rows, miss = [], 0
        t0 = time.time()
        for i, d in enumerate(days):
            j = ingest.load_raw(ds, d)
            if j is None:
                miss += 1; continue
            iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            rows.extend(parse.PARSERS[ds](j, iso))
            if verbose and (i + 1) % 400 == 0:
                print(f"  {ds}: {i+1}/{len(days)} 天, {len(rows):,} 列", flush=True)
        if not rows:
            print(f"  {ds}: 無資料，跳過"); continue
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        tbl = TABLES[ds]
        con.execute(f"DROP TABLE IF EXISTS {tbl}")
        con.register("_tmp", df)
        con.execute(f"CREATE TABLE {tbl} AS SELECT * FROM _tmp")
        con.unregister("_tmp")
        if verbose:
            print(f"  {ds} -> {tbl}: {len(df):,} 列, {df.date.nunique()} 天, "
                  f"缺 {miss} 天, {time.time()-t0:.0f}s", flush=True)
    # 索引
    for t in ("quotes", "inst", "valuation", "margin"):
        try: con.execute(f"CREATE INDEX IF NOT EXISTS ix_{t} ON {t}(date, code)")
        except Exception: pass
    con.close()


def q(sql, params=None):
    con = duckdb.connect(str(DB), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()


def summary():
    con = duckdb.connect(str(DB), read_only=True)
    out = []
    for t in ("quotes", "inst", "valuation", "margin"):
        try:
            r = con.execute(f"SELECT COUNT(*) n, MIN(date) a, MAX(date) b, "
                            f"COUNT(DISTINCT date) d FROM {t}").fetchone()
            out.append(f"  {t:<10} {r[0]:>9,} 列  {r[3]:>5} 天  {r[1]:%Y-%m-%d} ~ {r[2]:%Y-%m-%d}")
        except Exception:
            out.append(f"  {t:<10} (尚未建立)")
    con.close()
    return "\n".join(out)


if __name__ == "__main__":
    ds = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    build(ds)
    print(summary())
