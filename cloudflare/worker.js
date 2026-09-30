/* tw-stock-monitor 的 Cloudflare Worker：網頁「更新」的後端、代讀 PTT
 *
 * 1. 網頁的更新（POST /update、GET /status）
 *    網站沒有任何定時排程（2026-10-01 起全部拿掉）。資料只在兩種時候更新：
 *      - 使用者按右上角 ⟳（manual）：先在瀏覽器裡讀 PTT 算好社群聲量，跟著送進來；
 *        資料沒變時 CI 走快速通道，只重算社群聲量。
 *      - 有人打開網頁、網頁發現「照時間應該有新資料了，但網站還是舊的」（auto）：
 *        帶 trigger=cron，ci_update.py 比照舊的排程處理 —— 證交所還沒發完就安靜略過、
 *        今天已經發佈過就不重做。
 *    GitHub 金鑰只放在這裡（Secret），瀏覽器裡沒有任何金鑰、不用輸入任何東西。
 *    這兩個網址是公開的、不需要密碼（使用者的決定），所以：
 *      - 有更新在跑或排隊，就不再觸發
 *      - 距離上一次觸發不到 COOLDOWN_SEC 秒，就不再觸發
 *      - 只能觸發這一個 workflow，參數只有 trigger 與 social
 *    帶進來的社群聲量只限制大小，內容由 social.apply_payload 檢查。
 *
 * 2. 代讀 PTT 股票板（GET /ptt/bbs/Stock/…）
 *    PTT 整站在 Cloudflare 後面，GitHub 雲端主機直接連、或從 GitHub 呼叫這裡代讀都是 403
 *    （2026-09-30 實測：PTT 的防護看的是最初呼叫 Worker 的來源）；從一般使用者的瀏覽器呼叫則是 200。
 *    所以由網頁在使用者的瀏覽器裡透過這裡讀 PTT、算出聲量，再跟著 /update 一起送進來。
 *    只放行股票板的列表頁與文章頁，其他路徑一律 404 —— 這不是通用的代理。
 *    不登入、不帶任何帳號，只帶「已滿 18 歲」的確認。
 *
 * Secret：GITHUB_TOKEN —— Fine-grained token，只給這個 repo 的 Actions: Read and write。
 * 部署與設定步驟見 cloudflare/README.md。
 */
const COOLDOWN_SEC = 300;        // 兩次觸發至少隔 5 分鐘（一次更新本身就要 5–8 分鐘）
const MAX_SOCIAL = 60000;        // workflow_dispatch 的 inputs 總長上限是 65,535 個字元

// 只有自己的網站（與本機開發）的網頁可以從瀏覽器呼叫。這擋不住直接用程式打的人，
// 只是不讓別的網站借使用者的瀏覽器來打。
const ORIGINS = ["https://hsinfuyeh.github.io", "http://localhost:5500", "http://127.0.0.1:5500",
                 "http://localhost:5000", "http://127.0.0.1:5000"];

function cors(req) {
  const o = req.headers.get("Origin") || "";
  return {
    "Access-Control-Allow-Origin": ORIGINS.includes(o) ? o : ORIGINS[0],
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

function json(req, obj, status) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: Object.assign({ "content-type": "application/json; charset=utf-8", "cache-control": "no-store" }, cors(req)),
  });
}

export default {
  async fetch(req, env) {
    const path = new URL(req.url).pathname;
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(req) });
    if (req.method === "GET" && path.startsWith("/ptt/")) return pttRelay(req, path.slice(4));
    if (req.method === "GET" && path === "/status") return json(req, await latestRun(env));
    if (req.method === "POST" && path === "/update") return update(req, env);
    return new Response(
      "tw-stock-monitor：定時請 GitHub Actions 更新資料、網頁更新按鈕的後端、代讀 PTT 股票板。\n",
      { headers: { "content-type": "text/plain; charset=utf-8" } });
  },
};

/* ---------- GitHub ---------- */
function gh(env, path, init) {
  init = init || {};
  init.headers = {
    "Authorization": "Bearer " + env.GITHUB_TOKEN,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "tw-stock-monitor-cron",            // GitHub API 沒有 User-Agent 會直接拒絕
  };
  return fetch("https://api.github.com/repos/" + env.GH_REPO + "/actions/workflows/" + env.GH_WORKFLOW + path, init);
}

async function dispatch(env, inputs) {
  if (!env.GITHUB_TOKEN) return { ok: false, error: "沒有設定 GITHUB_TOKEN（npx wrangler secret put GITHUB_TOKEN）" };
  const r = await gh(env, "/dispatches", { method: "POST", body: JSON.stringify({ ref: "main", inputs: inputs }) });
  if (r.status === 204) return { ok: true };
  // 401／403：金鑰過期或權限不對；404：repo 或 workflow 名稱錯
  return { ok: false, error: "GitHub 回應 " + r.status + "：" + (await r.text()).slice(0, 200) };
}

/* 最近一次執行。只回網頁需要的幾個欄位。 */
async function latestRun(env) {
  if (!env.GITHUB_TOKEN) return { error: "沒有設定 GITHUB_TOKEN" };
  const r = await gh(env, "/runs?per_page=1");
  if (!r.ok) return { error: "GitHub 回應 " + r.status };
  const run = ((await r.json()).workflow_runs || [])[0];
  if (!run) return { run: null };
  return { run: { id: run.id, status: run.status, conclusion: run.conclusion,
                  created_at: run.created_at, url: run.html_url } };
}

/* 網頁按「更新」：有在跑就回報進行中，太頻繁就請對方等一下，否則觸發。 */
async function update(req, env) {
  let body = {};
  try { body = await req.json(); } catch (e) { /* 沒帶內容也可以，只觸發更新 */ }
  let social = "";
  if (body && body.social && typeof body.social === "object") {
    social = JSON.stringify(body.social);
    if (social.length > MAX_SOCIAL) social = "";       // 太大就不帶（只更新資料），不讓整次觸發失敗
  }
  const last = await latestRun(env);
  if (last.error) return json(req, { state: "error", message: last.error }, 502);
  const run = last.run;
  if (run && run.status !== "completed") return json(req, { state: "busy", run: run });
  if (run) {
    const wait = COOLDOWN_SEC - (Date.now() - Date.parse(run.created_at)) / 1000;
    if (wait > 0) return json(req, { state: "cooldown", wait: Math.ceil(wait), run: run });
  }
  // auto：網頁自己發現資料過期而觸發的，比照舊的排程（資料沒齊就略過）；不帶社群聲量
  const inputs = { trigger: body && body.auto ? "cron" : "manual" };
  if (social && inputs.trigger === "manual") inputs.social = social;
  const r = await dispatch(env, inputs);
  if (!r.ok) return json(req, { state: "error", message: r.error }, 502);
  return json(req, { state: "started", social: !!social, since: new Date().toISOString() });
}

/* ---------- PTT ---------- */
const PTT_PATH = /^\/bbs\/Stock\/(index\d*|M\.\d+\.A\.[0-9A-F]+)\.html$/;

async function pttRelay(req, path) {
  if (!PTT_PATH.test(path)) return new Response("not found\n", { status: 404, headers: cors(req) });
  const r = await fetch("https://www.ptt.cc" + path, {
    headers: { "User-Agent": "Mozilla/5.0 (compatible; personal-research/1.0)", "Cookie": "over18=1" },
  });
  // 狀態碼原樣傳回去：PTT 擋下來（403）的時候網頁才知道，會改成只更新資料
  return new Response(r.body, {
    status: r.status,
    headers: Object.assign({ "content-type": r.headers.get("content-type") || "text/html; charset=utf-8" }, cors(req)),
  });
}
