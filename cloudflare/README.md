# Cloudflare Worker：網頁更新的後端

網站沒有任何定時排程（2026-10-01 起）。資料只在有人打開網頁、或按右上角 ⟳ 時更新，
兩者都呼叫這個 Worker，由它用保管的 GitHub 金鑰觸發 GitHub Actions（瀏覽器裡沒有任何金鑰）：

| 網址 | 做什麼 |
|---|---|
| `POST /update` | 觸發更新。`{"auto": true}` 是網頁發現資料過期而自動觸發（比照舊的排程：資料沒齊就略過）；按 ⟳ 的會帶瀏覽器算好的社群聲量 |
| `GET /status` | 最近一次更新的進度，網頁用來顯示「更新中／完成」 |
| `GET /ptt/bbs/Stock/…` | 代讀 PTT 股票板（只放行列表頁與文章頁，其他 404；不登入、不帶帳號） |
| `GET /live` | 盤中排行頁用的即時報價：上市股票＋ETF（證交所 mis.twse.com.tw，分 14 批代讀）。暫存 30 秒（收盤後 10 分鐘），很多人同時看也只打證交所一輪；股票清單取自網站的 universe.json，不接受網頁指定代號 |

入口是公開的、不用密碼（使用者的決定），所以有更新在跑、或 5 分鐘內觸發過，就不再觸發。

PTT 對 GitHub 雲端主機一律 403，從 GitHub 呼叫這個 Worker 代讀也一樣 403；
只有從使用者的瀏覽器透過這裡讀是通的，所以社群聲量是網頁在按 ⟳ 時讀、算好再送進來。

`wrangler.toml` 的 `crons = []` 是刻意的：寫成空的，部署時才會把 Cloudflare 上原本登記的排程一併刪掉。

## 第一次設定（只做一次）

### 1. 建一把 GitHub 金鑰（只能觸發更新、讀進度）

到 GitHub → Settings → Developer settings → [Fine-grained tokens](https://github.com/settings/personal-access-tokens/new)：

- Token name：`tw-stock-monitor-cron`
- Expiration：**No expiration**（不會過期。2026-10-29 金鑰過期，網頁更新停了好幾天才發現；這把金鑰只能觸發這一個 repo 的更新、讀進度，外流的風險有限。有設期限的話，剩 14 天時網頁會提醒）
- Repository access：Only select repositories → `tw-stock-monitor`
- Permissions → Repository permissions → **Actions: Read and write**（其他都不用開）

按 Generate，複製 `github_pat_…` 開頭的那串（只會顯示一次）。

### 2. 登入 Cloudflare 並部署

```bash
cd cloudflare
npx wrangler login
npx wrangler deploy
```

`wrangler login` 會開瀏覽器，登入（沒有帳號就先免費註冊）並按 Allow。

### 3. 把金鑰放進 Cloudflare

```bash
npx wrangler secret put GITHUB_TOKEN
```

它會要你貼上第 1 步的金鑰（畫面上不會顯示），按 Enter。金鑰只存在 Cloudflare，不會進 repo。

### 4. 真人驗證 Turnstile（選用，2026-10-03 加入）

更新入口是公開的，Turnstile 擋掉送假社群聲量的人；一般瀏覽器不用做任何事。
2026-11-02 起**沒通過也照樣更新資料**，只是不收那次送來的社群聲量（網頁會註明）：以前沒過就整個擋掉，真人也常因為被判成機器人而按不動。單純觸發更新有「在跑就不再觸發、5 分鐘冷卻」限住。

1. Cloudflare 後台 → **Turnstile** → **Add widget**
   - Widget name：`tw-stock-monitor`
   - Hostname：`hsinfuyeh.github.io`
   - Widget Mode：**Invisible**（完全不跳框；選 Managed 的話，可疑時右下角會出現勾選框）
2. 建立後會看到兩把：
   - **Site Key**（公開的）：填進 `site/assets/app.js` 的 `TURNSTILE_SITEKEY`，commit 推上去，等網站更新完。
   - **Secret Key**（密鑰）：**網站換成新版之後**才在這個資料夾跑 `npx wrangler secret put TURNSTILE_SECRET`
     貼上。順序反過來的話，舊網頁拿不到通行證，那段時間的更新不會帶社群聲量。
3. 要停用：`npx wrangler secret delete TURNSTILE_SECRET`（Worker 沒有密鑰就不檢查）。

本機預覽（`localhost:5500?cloud=1`）不在 Hostname 裡，從本機預覽按 ⟳ 只會更新資料、不帶社群聲量；本機用 `python server.py` 的更新不經過 Worker，不受影響。

## 平常要看的

- **更新紀錄**：GitHub → Actions → 更新資料並發佈。觸發者都是 `workflow_dispatch`；
  自動觸發的大部分會是「沒有發佈（已是最新／資料還沒齊）」的綠燈，這是正常的。
- **觸發失敗的原因**：Cloudflare 後台 → Workers & Pages → `tw-stock-monitor-cron` → Logs。
  網頁會直接用中文說明（例如「GitHub 金鑰需要更新」＋換金鑰的步驟）；打開網頁時也會先檢查一次，
  金鑰過期、快到期、上一次更新失敗（含失敗在哪一步）、上一次有附加資料沒抓到，都會顯示在上方訊息列。
  金鑰過期或權限不對：照第 1、3 步換一把（第 1 步選 No expiration）。
- **改了 worker.js 或 wrangler.toml**：在這個資料夾跑 `npx wrangler deploy`。
