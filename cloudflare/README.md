# Cloudflare 定時觸發器

GitHub Actions 內建的排程（schedule）在免費方案常常延遲或整個略過：2026-09-21～29
每個交易日排 5 次，下午那 4 次一次都沒跑，網站多半到晚上 8 點才更新。

這個 Worker（Cloudflare 上的小程式）每個交易日準時「按」GitHub 的更新按鈕：

| 台北時間 | 做什麼 |
|---|---|
| 14:10–19:40 每半小時 | 資料一齊就發佈；當天已經發佈過的後面幾次直接略過 |
| 21:50、22:40 | 融資券公布後，用完整資料重新產出名單 |

真正抓資料、產網站的還是 GitHub Actions，這裡只負責準時觸發。
不用任何電腦開著。休市日也會觸發，但 `ci_update.py` 發現沒有新的交易日就安靜略過。
GitHub 原本的 schedule 保留當備援。

## 第一次設定（只做一次）

### 1. 建一把 GitHub 金鑰（只能觸發更新）

到 GitHub → Settings → Developer settings → [Fine-grained tokens](https://github.com/settings/personal-access-tokens/new)：

- Token name：`tw-stock-monitor-cron`
- Expiration：最長 1 年（到期前要換一把，見下方）
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

## 平常要看的

- **有沒有準時觸發**：GitHub → Actions → 更新資料並發佈，觸發者是 `workflow_dispatch`、
  每半小時一筆。大部分會是「沒有發佈（已是最新／資料還沒齊）」的綠燈，這是正常的。
- **觸發失敗的原因**：Cloudflare 後台 → Workers & Pages → `tw-stock-monitor-cron` → Logs。
  `401`／`403` 代表金鑰過期或權限不對，照第 1、3 步換一把。
- **改時間**：改 `wrangler.toml` 的 `[triggers]`（UTC 時間），再 `npx wrangler deploy`。
