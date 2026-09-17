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
    """從 raw/ 全量重建。

    寫到暫存檔再原子換掉，不是就地 DROP/CREATE。兩個理由：

    1. <b>DuckDB 不會就地回收空間</b>。反覆 DROP TABLE 再 CREATE TABLE，
       檔案只會一直長 —— 實測同一份資料，就地重建過幾十次之後是 873 MB，
       寫成新檔只有 143 MB，膨脹了 6 倍。
    2. 重建到一半掛掉時，舊的資料庫還完好無損；換檔是一瞬間的事。

    只重建部分資料集時不能換檔（會丟掉沒重建的表），那種情況退回就地更新。
    """
    datasets = datasets or list(DATASETS)
    days = ingest.trading_calendar()
    full = set(datasets) == set(DATASETS)
    target = DB.with_suffix(".building.duckdb") if full else DB
    if full and target.exists():
        target.unlink()
    con = duckdb.connect(str(target))
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
    # 刻意不建索引。實測這四個 (date, code) 索引佔掉 407 MB —— 整個檔案的 74% ——
    # 而且沒有任何好處：全表掃描 1.53s（有索引時反而是 1.98s），單檔查詢
    # 0.093s vs 0.083s 在誤差內。
    # 合理：DuckDB 是欄式儲存且有 zone map，這裡的查詢又幾乎都是整表掃進
    # pandas，ART 索引只是白佔空間。
    con.close()
    if full:
        import os
        os.replace(str(target), str(DB))
        if verbose:
            print(f"  倉儲 {DB.stat().st_size/1e6:.0f} MB", flush=True)


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
