/* tw-stock-monitor 定時觸發器（Cloudflare Worker）
 *
 * 為什麼需要：GitHub Actions 內建的 schedule 在免費方案常常延遲或整個略過 ——
 * 2026-09-21～29 每天排 5 次，下午那 4 次一次都沒跑，網站多半到晚上 8 點才更新。
 * Cloudflare 的 Cron Trigger 準到分鐘，所以改由這裡準時「按」GitHub 的更新按鈕
 * （workflow_dispatch），真正抓資料、產網站的還是 GitHub Actions。
 *
 * 帶 trigger=cron：ci_update.py 會比照排程處理 —— 資料還沒齊就安靜略過、
 * 當天已經發佈過就不重做，所以每半小時觸發一次也不會重複產出。
 *
 * 時間表在 wrangler.toml 的 [triggers]（UTC；台北 = UTC+8）。
 * 需要的 Secret：GITHUB_TOKEN —— Fine-grained token，只給這個 repo 的 Actions: Read and write。
 * 部署與設定步驟見 cloudflare/README.md。
 */
export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env, event.cron));
  },

  // 只回一段說明。刻意不提供「從網址觸發」—— 否則任何人打這個網址都能叫 GitHub 跑更新。
  async fetch() {
    return new Response(
      "tw-stock-monitor 定時觸發器：每個交易日定時請 GitHub Actions 更新資料。這個網址不接受觸發。\n",
      { headers: { "content-type": "text/plain; charset=utf-8" } });
  },
};

async function dispatch(env, cron) {
  if (!env.GITHUB_TOKEN) {
    console.error("沒有設定 GITHUB_TOKEN（npx wrangler secret put GITHUB_TOKEN），這次不觸發");
    return;
  }
  const url = "https://api.github.com/repos/" + env.GH_REPO +
    "/actions/workflows/" + env.GH_WORKFLOW + "/dispatches";
  const r = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": "Bearer " + env.GITHUB_TOKEN,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "tw-stock-monitor-cron",          // GitHub API 沒有 User-Agent 會直接拒絕
    },
    body: JSON.stringify({ ref: "main", inputs: { trigger: "cron" } }),
  });
  if (r.status === 204) {
    console.log("已觸發 GitHub 更新（" + cron + "）");
  } else {
    // 401／403：金鑰過期或權限不對；404：repo 或 workflow 名稱錯。記在 Worker 的 Logs 裡。
    console.error("觸發失敗 " + r.status + "（" + cron + "）：" + (await r.text()).slice(0, 300));
  }
}
