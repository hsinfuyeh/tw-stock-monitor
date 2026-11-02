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
 *      - 設了 TURNSTILE_SECRET 就檢查 Cloudflare Turnstile（真人驗證）：網頁在背景拿一張一次性
 *        通行證跟著送來，這裡拿去 Cloudflare 驗。沒過照樣更新資料，只是不收這次的社群聲量
 *        （擋的是送假社群聲量的人）。一般瀏覽器什麼都不用做。沒設就不檢查。
 *    出錯一律回 { state: "error", kind, message }，message 是給人看的中文（金鑰過期要怎麼換等等）。
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
 *         TURNSTILE_SECRET —— Turnstile 小工具的密鑰（選用，設了才檢查真人驗證）。
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

// 同一個 Worker 執行個體剛觸發過，就先不再觸發：觸發後要過幾秒，新的執行才會出現在 GitHub 的清單裡，
// 那幾秒內同時按下的人會看到「上一次是很久以前」而各自再觸發一次。
let lastDispatchAt = 0;

export default {
  async fetch(req, env) {
    try {
      const path = new URL(req.url).pathname;
      if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(req) });
      if (req.method === "GET" && path.startsWith("/ptt/")) return await pttRelay(req, path.slice(4));
      if (req.method === "GET" && path === "/status") return json(req, await latestRun(env, true));
      if (req.method === "POST" && path === "/update") return await update(req, env);
      return new Response(
        "tw-stock-monitor：網頁更新按鈕的後端、代讀 PTT 股票板。\n",
        { headers: { "content-type": "text/plain; charset=utf-8" } });
    } catch (e) {
      // 任何沒料到的錯誤也要回帶 CORS 的 JSON：否則瀏覽器只看得到「Failed to fetch」，不知道是哪裡壞了
      console.log("worker error:", (e && e.stack) || e);
      const msg = "更新服務內部錯誤：" + ((e && e.message) || e);
      return json(req, { state: "error", kind: "worker", error: msg, message: msg }, 500);
    }
  },
};

/* ---------- GitHub ---------- */
const TOKEN_FIX = "請到 GitHub 的 Fine-grained tokens 頁面重新產生 tw-stock-monitor-cron 這把金鑰" +
  "（Expiration 選 No expiration），再在 cloudflare 資料夾執行 npx wrangler secret put GITHUB_TOKEN。";

/* GitHub API。暫時故障（502／503／504、連不上）隔 1.5 秒再試一次 —— GitHub 偶爾會這樣，
   不該因此讓使用者看到失敗。 */
async function ghApi(env, path, init) {
  init = init || {};
  init.headers = {
    "Authorization": "Bearer " + env.GITHUB_TOKEN,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "tw-stock-monitor-cron",            // GitHub API 沒有 User-Agent 會直接拒絕
  };
  const url = "https://api.github.com/repos/" + env.GH_REPO + path;
  for (let i = 0; ; i++) {
    try {
      const r = await fetch(url, init);
      if (i >= 1 || ![502, 503, 504].includes(r.status)) return r;
    } catch (e) {
      if (i >= 1) throw e;
    }
    await new Promise(ok => setTimeout(ok, 1500));
  }
}

function gh(env, path, init) {
  return ghApi(env, "/actions/workflows/" + env.GH_WORKFLOW + path, init);
}

/* GitHub 的錯誤 -> { kind, message }。kind 給網頁決定要不要打擾使用者：
     token   金鑰過期或權限不對（要使用者自己處理，一定要講）
     config  repo／workflow 名稱或參數不對
     rate    API 額度暫時用完
     github  GitHub 暫時故障或連不上 */
async function explain(r, env) {
  let text = "";
  try { text = (await r.text()).slice(0, 300); } catch (e) { /* 讀不到內容也照樣分類 */ }
  const s = r.status;
  if (s === 401) return { kind: "token", message: "GitHub 金鑰失效（多半是過期了）。" + TOKEN_FIX };
  if (s === 429 || (s === 403 && (r.headers.get("x-ratelimit-remaining") === "0" || /rate limit/i.test(text))))
    return { kind: "rate", message: "GitHub 的 API 額度暫時用完了，大約一小時內會恢復，晚點再按。" };
  if (s === 403) return { kind: "token", message: "GitHub 金鑰的權限不夠（需要 " + env.GH_REPO + " 的 Actions 讀寫）。" + TOKEN_FIX };
  if (s === 404) return { kind: "config", message: "GitHub 找不到 " + env.GH_REPO + " 或 " + env.GH_WORKFLOW +
    "：金鑰沒有包含這個 repo，或 wrangler.toml 的 GH_REPO、GH_WORKFLOW 設錯了。" };
  if (s === 422) return { kind: "config", message: "GitHub 拒絕這次觸發（main 分支上的 workflow 參數對不上）：" + text };
  if (s >= 500) return { kind: "github", message: "GitHub 暫時故障（" + s + "），過幾分鐘再試。" };
  return { kind: "github", message: "GitHub 回應 " + s + "：" + text };
}

/* 金鑰還有幾天到期。有設期限的金鑰，GitHub 每個回應都會帶這個標頭（例如 2027-11-02 12:00:00 +0800）；
   設 No expiration 就沒有，回 null。網頁快到期時會提醒，不必等到更新失敗才知道。 */
function tokenDays(r) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(r.headers.get("github-authentication-token-expiration") || "");
  if (!m) return null;
  return Math.floor((Date.UTC(+m[1], +m[2] - 1, +m[3]) - Date.now()) / 86400e3);
}

async function dispatch(env, inputs) {
  if (!env.GITHUB_TOKEN) return { ok: false, kind: "token", error: "沒有設定 GITHUB_TOKEN。" + TOKEN_FIX };
  let r;
  try {
    r = await gh(env, "/dispatches", { method: "POST", body: JSON.stringify({ ref: "main", inputs: inputs }) });
  } catch (e) {
    return { ok: false, kind: "github", error: "連不上 GitHub，過幾分鐘再試。" };
  }
  if (r.status === 204) return { ok: true };
  const x = await explain(r, env);
  return { ok: false, kind: x.kind, error: x.message };
}

/* 最近一次執行。只回網頁需要的幾個欄位。
   detail：失敗的話再問一次是哪一步失敗（網頁 /status 才需要；/update 只要知道有沒有在跑）。 */
async function latestRun(env, detail) {
  if (!env.GITHUB_TOKEN) return { error: "沒有設定 GITHUB_TOKEN。" + TOKEN_FIX, kind: "token" };
  let r;
  try {
    r = await gh(env, "/runs?per_page=1");
  } catch (e) {
    return { error: "連不上 GitHub，過幾分鐘再試。", kind: "github" };
  }
  if (!r.ok) {
    const x = await explain(r, env);
    return { error: x.message, kind: x.kind };
  }
  const out = { token_days: tokenDays(r) };
  const run = ((await r.json()).workflow_runs || [])[0];
  if (!run) return Object.assign(out, { run: null });
  out.run = { id: run.id, status: run.status, conclusion: run.conclusion,
              created_at: run.created_at, url: run.html_url };
  if (detail && run.status === "completed" && run.conclusion === "failure") out.run.failed_step = await failedStep(env, run.id);
  return out;
}

/* 失敗的是哪一步（例如「抓取新資料」「產出網站」），讓網頁照實說，而不是一律猜「證交所還沒發完」。 */
async function failedStep(env, id) {
  try {
    const r = await ghApi(env, "/actions/runs/" + id + "/jobs?per_page=10");
    if (!r.ok) return null;
    for (const job of (await r.json()).jobs || []) {
      if (job.conclusion !== "failure") continue;
      const st = (job.steps || []).find(s => s.conclusion === "failure");
      return st ? st.name : job.name;
    }
  } catch (e) { /* 問不到就不說是哪一步 */ }
  return null;
}

/* 網頁按「更新」：有在跑就回報進行中，太頻繁就請對方等一下，否則觸發。 */
async function update(req, env) {
  let body = {};
  try { body = await req.json(); } catch (e) { /* 沒帶內容也可以，只觸發更新 */ }
  if (!body || typeof body !== "object") body = {};
  let social = "";
  if (body.social && typeof body.social === "object") {
    social = JSON.stringify(body.social);
    if (social.length > MAX_SOCIAL) social = "";       // 太大就不帶（只更新資料），不讓整次觸發失敗
  }
  const last = await latestRun(env, false);
  if (last.error) return json(req, { state: "error", kind: last.kind, message: last.error }, 502);
  const run = last.run;
  if (run && run.status !== "completed") return json(req, { state: "busy", run: run });
  const since = Math.max(run ? Date.parse(run.created_at) : 0, lastDispatchAt);
  const wait = COOLDOWN_SEC - (Date.now() - since) / 1000;
  if (wait > 0) return json(req, { state: "cooldown", wait: Math.ceil(wait), run: run });
  // 真人驗證沒過（被判成機器人、驗證程式被擋、通行證過期…）也照樣更新資料，只是不收這次送來的社群聲量：
  // Turnstile 要擋的主要是「送假的社群聲量」；單純觸發更新，上面已經有「在跑就不再觸發、5 分鐘冷卻」限住，
  // 被亂按也只是多跑幾次。以前沒過就整個擋掉，結果真人也常常因此按不動。
  const hv = await human(req, env, body.turnstile);
  if (!hv.ok) console.log("turnstile rejected:", hv.why);
  // auto：網頁自己發現資料過期而觸發的，比照舊的排程（資料沒齊就略過）；不帶社群聲量
  const inputs = { trigger: body.auto ? "cron" : "manual" };
  if (social && inputs.trigger === "manual" && hv.ok) inputs.social = social;
  const r = await dispatch(env, inputs);
  if (!r.ok) return json(req, { state: "error", kind: r.kind, message: r.error }, 502);
  lastDispatchAt = Date.now();
  const out = { state: "started", social: !!inputs.social, since: new Date().toISOString() };
  if (!hv.ok) out.human = hv.why;
  return json(req, out);
}

/* ---------- Turnstile ---------- */
/* 網頁送來的通行證拿去 Cloudflare 驗。沒設密鑰（還沒啟用）就放行。
   通行證只能用一次、5 分鐘過期；action 要是網頁那邊指定的 update，別的地方拿到的不算。
   回傳 { ok, why }：why 是沒過的原因（不含任何金鑰），會顯示在網頁上，方便查是哪一段出問題 —— 
   「網頁沒拿到通行證」跟「通行證被 Cloudflare 退回（例如密鑰貼錯）」要分得出來。 */
async function human(req, env, token) {
  if (!env.TURNSTILE_SECRET) return { ok: true };
  if (!token || typeof token !== "string") return { ok: false, why: "網頁沒有拿到通行證" };
  if (token.length > 2048) return { ok: false, why: "通行證格式不對" };
  const form = new FormData();
  form.append("secret", env.TURNSTILE_SECRET);
  form.append("response", token);
  const ip = req.headers.get("CF-Connecting-IP");
  if (ip) form.append("remoteip", ip);
  try {
    const r = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", { method: "POST", body: form });
    const j = await r.json();
    if (!j.success) return { ok: false, why: "Cloudflare 退回：" + ((j["error-codes"] || []).join(",") || "未知") };
    if (j.action !== "update") return { ok: false, why: "action 不符：" + j.action };
    return { ok: true };
  } catch (e) {
    return { ok: false, why: "連不上 Cloudflare 驗證服務" };
  }
}

/* ---------- PTT ---------- */
const PTT_PATH = /^\/bbs\/Stock\/(index\d*|M\.\d+\.A\.[0-9A-F]+)\.html$/;

async function pttRelay(req, path) {
  if (!PTT_PATH.test(path)) return new Response("not found\n", { status: 404, headers: cors(req) });
  let r;
  try {
    r = await fetch("https://www.ptt.cc" + path, {
      headers: { "User-Agent": "Mozilla/5.0 (compatible; personal-research/1.0)", "Cookie": "over18=1" },
    });
  } catch (e) {
    return new Response("PTT 連不上\n", { status: 502, headers: cors(req) });
  }
  // 狀態碼原樣傳回去：PTT 擋下來（403）的時候網頁才知道，會改成只更新資料
  return new Response(r.body, {
    status: r.status,
    headers: Object.assign({ "content-type": r.headers.get("content-type") || "text/html; charset=utf-8" }, cors(req)),
  });
}
