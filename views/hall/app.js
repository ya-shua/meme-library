const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let CONST = { standard_tags: {} };
const state = {
  packs: [],
  manage: { packId: "", tag: "", status: "", page: 1, size: 48,
            selected: new Set(), selPacks: new Set() },
  preview: { url: "", group: "", page: 1, size: 24, total: 0, summary: null, downloading: false },
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function api(path, opts = {}) {
  const { timeout, ...rest } = opts;
  let timer = null;
  let ctrl = null;
  if (timeout) {
    ctrl = new AbortController();
    timer = setTimeout(() => ctrl.abort(), timeout);
  }
  try {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...rest,
      ...(ctrl ? { signal: ctrl.signal } : {}),
    });
    if (!resp.ok) throw new Error(await resp.text());
    return await resp.json();
  } finally {
    if (timer) clearTimeout(timer);
  }
}

const STAGE_TEXT = {
  queued: "排队中", downloading: "下载中", extracting: "解压中",
  cloning: "git 拉取中", importing: "入库中", done: "完成",
};

// 下载进度：下载阶段占前 70%，入库阶段占后 30%
function calcPct(d) {
  if (d.stage === "importing" && d.total) return 72 + (d.done_items / d.total) * 28;
  if (d.stage === "downloading" && d.total) return Math.min(70, (d.bytes / d.total) * 70);
  if (d.stage === "extracting") return 71;
  if (d.stage === "cloning") return 20;
  return 6;
}

function dlLabel(d) {
  const stage = STAGE_TEXT[d.stage] || d.stage || "准备中";
  const pct = Math.round(calcPct(d));
  if (d.stage === "importing" && d.total) return `${stage} ${pct}% · ${d.done_items}/${d.total} 张`;
  if (d.stage === "downloading") {
    const got = (d.bytes || 0) / 1048576;
    const all = (d.total || 0) / 1048576;
    return all >= 0.1
      ? `${stage} ${pct}% · ${got.toFixed(1)}/${all.toFixed(1)}MB`
      : `${stage} ${got.toFixed(1)}MB`;
  }
  return `${stage} ${pct}%`;
}

// ================= 自家确认框 / 轻提示 =================
function askConfirm({ title = "确认一下", text = "", okText = "确定", danger = false }) {
  return new Promise((resolve) => {
    const mask = $("#modal");
    const ok = $("#modal-ok");
    const cancel = $("#modal-cancel");
    $("#modal-title").textContent = title;
    $("#modal-text").textContent = text;
    ok.textContent = okText;
    ok.className = "btn " + (danger ? "danger" : "primary");
    mask.classList.remove("hidden");

    const done = (v) => {
      mask.classList.add("hidden");
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      mask.removeEventListener("click", onMask);
      document.removeEventListener("keydown", onKey);
      resolve(v);
    };
    const onOk = () => done(true);
    const onCancel = () => done(false);
    const onMask = (e) => { if (e.target === mask) done(false); };
    const onKey = (e) => { if (e.key === "Escape") done(false); };
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    mask.addEventListener("click", onMask);
    document.addEventListener("keydown", onKey);
  });
}

let toastTimer = null;
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 2200);
}

// ================= 导航 =================
$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

function switchView(view) {
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "overview") loadOverview();
  if (view === "manage") loadManage();
  if (view === "dl") loadDownloads();
  if (view === "trash") loadTrash();
  if (view === "review") loadReview();
  if (view === "settings") loadMoods();
}

// ================= 常量 =================
async function loadConstants() {
  CONST = await api("/api/constants");
  const opts = Object.entries(CONST.standard_tags)
    .map(([k, v]) => `<option value="${k}">${v}（${k}）</option>`).join("");
  $("#mood-tag").innerHTML = opts;
  $("#batch-tag").innerHTML = opts;
}

// ================= 侧栏统计 =================
async function refreshSideStats() {
  const ov = await api("/api/overview");
  const total = Object.values(ov.review).reduce((a, b) => a + b, 0);
  const usable = Object.values(ov.tag_stats).filter((n) => n > 0).length;
  $("#foot-total").textContent = total;
  $("#foot-tags").textContent = `${usable} / ${Object.keys(CONST.standard_tags).length}`;
}

// ================= 资源广场 =================
// 表情包仓库：地址抄在插件里，不依赖外部索引；卡片带真封面
const REPO_EMOJI = ["🐟", "🎭", "🎨", "💫", "🌸", "🍀", "🌊", "🐱", "⭐",
                    "🎐", "🦊", "🍡", "💭", "🕰️", "🧹", "🍰", "📚"];

async function loadStoreIndex() {
  const box = $("#store-index");
  try {
    const r = await api("/api/store/repos");
    if (!r.ok) { $("#store-msg").textContent = r.message || "地址库读不到"; return; }

    box.innerHTML = r.items.map((it, i) => `
      <div class="pack-card" data-repo="${esc(it.repo)}" data-ref="${esc(it.ref)}"
           data-label="${esc(it.name)}"
           data-url="${esc(it.url)}" data-exists="${it.exists_locally ? 1 : 0}">
        <div class="cover loading"><span class="cover-emoji">${REPO_EMOJI[i % REPO_EMOJI.length]}</span></div>
        <div class="pack-body">
          <h3 title="${esc(it.name)}">${esc(it.name)}</h3>
          <p class="pack-desc" title="${esc(it.note)}">${esc(it.note)}</p>
        </div>
        <div class="pack-foot">
          ${it.exists_locally
            ? `<button class="btn soft" data-act="preview">打开看</button>`
            : `<button class="btn soft" data-act="install">下载</button>
               <button class="btn soft2" data-act="preview">预览</button>`}
        </div>
      </div>`).join("") || `<p class="msg">地址库是空的。</p>`;

    $$("#store-index [data-act]").forEach((b) => {
      const card = b.closest(".pack-card");
      if (b.dataset.act === "preview") {
        b.addEventListener("click", () => {
          $("#store-url").value = card.dataset.url;
          doInspect();
        });
      } else if (b.dataset.act === "install") {
        b.addEventListener("click", () => startInstall(card));
      }
    });

    $$("#store-index .pack-card").forEach(askCover);
  } catch (e) {
    $("#store-msg").textContent = "地址库读不到：" + e.message;
  }
}

// 封面：先用索引自带封面顶着，再逐个换成包里真实的一张表情（3 路并发）
const coverPending = [];
let coverActive = 0;

function askCover(card) {
  if (!card.querySelector(".cover")) return;   // 没有封面位的卡片不请求
  coverPending.push(card);
  pumpCover();
}

function pumpCover() {
  while (coverActive < 6 && coverPending.length) {
    const card = coverPending.shift();
    coverActive++;
    const repo = card.dataset.repo;
    const ref = card.dataset.ref || "main";
    const cover = card.querySelector(".cover");
    fetch(`/api/store/cover?repo=${encodeURIComponent(repo)}&ref=${encodeURIComponent(ref)}`)
      .then((r) => r.json())
      .then((d) => {
        if (!d.ok || !d.url || !cover) return;
        const pre = new Image();
        pre.onload = () => {
          cover.innerHTML = `<img src="${d.url}" alt="" />`;   // 换成包里真实的一张表情
          cover.classList.remove("loading");
        };
        pre.onerror = () => cover.classList.remove("loading");
        pre.src = d.url;
      })
      .catch(() => {})
      .finally(() => {
        coverActive--;
        pumpCover();
      });
  }
}

// 卡片上直接收录进库：走 zip 下载，不占 GitHub API 额度
async function startInstall(card) {
  const btn = card.querySelector('[data-act="install"]');
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.textContent = "提交中…";
  try {
    const r = await api("/api/download", {
      method: "POST",
      body: JSON.stringify({
        url: card.dataset.url,
        type: "emotion",
        ref: card.dataset.ref || "",
      }),
    });
    if (!r.ok) throw new Error(r.message || "提交失败");
    btn.textContent = "下载中…";
    // 卡片直接下载也走同一个小窗，取消和后台都在里面
    openDlPop(r.task_id, card.dataset.label || "新包", "store");
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "下载";
    toast(`${card.dataset.label || "这个包"}：${e.message}`);
  }
}

// 下载成功后把那张卡片就地改成「打开看」，不贴任何状态标签
function markStoreCardDone(label) {
  const card = $$("#store-index .pack-card").find((c) => c.dataset.label === label);
  if (!card) return;
  card.dataset.exists = "1";
  const foot = card.querySelector(".pack-foot");
  if (foot) {
    foot.innerHTML = `<button class="btn soft" data-act="preview">打开看</button>`;
    foot.querySelector('[data-act="preview"]').addEventListener("click", () => {
      $("#store-url").value = card.dataset.url;
      doInspect();
    });
  }
  askCover(card);   // 换成包里真实的那张表情当封面
}

function showLoad(text = "读取仓库中……") {
  $("#loading-text").textContent = text;
  $("#loading").classList.remove("hidden");
}
function hideLoad() {
  $("#loading").classList.add("hidden");
}

async function doInspect() {
  const url = $("#store-url").value.trim();
  if (!url) { toast("先贴个仓库地址"); return; }
  $("#store-msg").textContent = "";
  closeDetail();
  showLoad("读仓库清单……大包第一次会慢一点，几十秒也正常");
  try {
    const r = await api(`/api/repo/inspect?url=${encodeURIComponent(url)}`, { timeout: 45000 });
    if (!r.ok) { $("#store-msg").textContent = r.message || "预览失败"; return; }

    if (!r.is_meme_pack) {
      const msg = $("#store-msg");
      msg.innerHTML = `<span class="warn-text">${esc(r.verdict)}</span>
        <button class="link-btn" id="force-preview">仍然预览</button>`;
      $("#force-preview").addEventListener("click", () => openDetail(r, url));
      return;
    }
    openDetail(r, url);
  } catch (e) {
    const timeout = e && e.name === "AbortError";
    $("#store-msg").textContent = timeout
      ? "读清单超时了。这个包多半超过 50MB，GitHub 接口这会儿也没空（没登录一小时只有 60 次）。"
        + "直接点「下载」，那条路走 zip 直下不占额度，装完再回来看。"
      : "预览失败：" + e.message;
  } finally {
    hideLoad();
  }
}

function openDetail(r, url) {
  state.preview = {
    url, group: "", page: 1, total: r.total, summary: r, downloading: false,
    size: 24,   // 一页 24 张：境外源一张十来秒，排太多只会互相拖死
  };
  renderDetailHead(r);
  $("#detail").classList.remove("hidden");
  $("#detail").scrollTop = 0;   // 每次进来从头看，不带上一次的滚动位置
  $("#detail-progress-text").classList.add("hidden");
  $("#store-msg").textContent = r.is_meme_pack
    ? `${r.verdict}　看中了就点右上角「下载入库」，想回去按左上角的 ←。`
    : `这个仓库不太像表情包，硬要看的话往下翻。共 ${r.total} 张图。`;
  loadDetailFiles();
}

function closeDetail() {
  $("#detail").classList.add("hidden");
  $("#detail-progress-text").classList.add("hidden");
}

function renderDetailHead(r) {
  // 只露我们自己的名字，仓库标识不往界面上放
  $("#detail-name").textContent = r.name || "这个表情包";
  $("#detail-platform").textContent =
    `外部源 · ${r.platform === "github" ? "GitHub" : "Gitee"}`;
  $("#detail-branch").textContent = r.branch;
  $("#detail-count").textContent = `${r.total} 张`;
  const native = $("#detail-native");
  native.textContent = r.has_native_tag ? `自带分类 ${r.native_tags.length} 组` : "无自带分类";
  native.className = "pill native " + (r.has_native_tag ? "on" : "off");
  $("#detail-desc").textContent = r.description || "";

  const warn = [];
  if (r.truncated) warn.push("仓库太大，文件树被截断，只列出了一部分。");
  if (r.exists_locally) warn.push("本地已有同名集合，下载会失败，先去「我的表情包」删掉旧的。");
  $("#detail-warn").textContent = warn.join(" ");
  $("#detail-download").disabled = !!r.exists_locally;
  $("#detail-download").textContent = r.exists_locally ? "已入库" : "下载入库";
  const pt = $("#detail-progress-text");
  if (pt) { pt.classList.add("hidden"); pt.textContent = ""; }
  renderDetailGroups(r.groups);
}

function renderDetailGroups(groups) {
  const entries = Object.entries(groups || {});
  const cur = state.preview.group;
  const all = `<button class="chip ${cur === "" ? "on" : ""}" data-group="">全部 ${state.preview.total}</button>`;
  const rest = entries.map(([g, c]) => {
    const name = CONST.standard_tags[g] ? `${CONST.standard_tags[g]} · ${g}` : g;
    return `<button class="chip ${cur === g ? "on" : ""}" data-group="${esc(g)}">${esc(name)} <span class="n">${c}</span></button>`;
  }).join("");
  $("#detail-groups").innerHTML = all + rest;
  $$("#detail-groups .chip").forEach((c) =>
    c.addEventListener("click", () => {
      state.preview.group = c.dataset.group;
      state.preview.page = 1;
      $$("#detail-groups .chip").forEach((x) => x.classList.remove("on"));
      c.classList.add("on");
      loadDetailFiles();
    }));
}

async function loadDetailFiles() {
  const q = new URLSearchParams({
    url: state.preview.url, group: state.preview.group,
    page: state.preview.page, size: state.preview.size,
  });
  const r = await api(`/api/repo/files?${q}`);
  if (!r.ok) { $("#store-msg").textContent = r.message || "加载失败"; return; }
  state.preview.total = r.total;

  $("#detail-grid").innerHTML = r.items.map((it) => `
    <div class="tile loading" title="${esc(it.path)}">
      <img class="thumb" data-src="${it.thumb}" alt="" />
      <div class="thumb-fail">缩略图加载失败</div>
      <div class="tile-body">
        <div class="tagline">${it.tag
          ? `<span class="mini-tag">${esc(CONST.standard_tags[it.tag] || it.tag)}</span>`
          : `<span class="mini-tag" style="color:#8d97a8;background:#181d27">未分类</span>`}</div>
        <div class="filen">${esc(it.name)}</div>
      </div>
    </div>`).join("") || `<p class="msg">这个分组下没有图。</p>`;

  $("#detail-pageinfo").textContent =
    `${r.page} / ${Math.max(1, Math.ceil(r.total / state.preview.size))} · 共 ${r.total} 张`;

  queueThumbs($("#detail-grid"));
}

// 限速链路上一次拉太多原图会互相拖死：只开 3 路，失败重试一次
function queueThumbs(root) {
  const imgs = Array.from(root.querySelectorAll("img[data-src]"));
  let cursor = 0;
  const pull = () => {
    if (cursor >= imgs.length) return;
    const img = imgs[cursor++];
    const src = img.dataset.src;
    let tries = 0;
    const load = () => {
      const tile = img.closest(".tile") || img.closest(".cover");
      img.onload = () => {
        img.removeAttribute("data-src");
        tile?.classList.remove("loading");
        pull();
      };
      img.onerror = () => {
        if (tries++ < 1) {
          setTimeout(load, 1200);
        } else {
          tile?.classList.remove("loading");
          tile?.classList.add("fail");
          pull();
        }
      };
      img.src = src;
    };
    load();
  };
  for (let i = 0; i < 6; i++) pull();
}

$("#store-preview").addEventListener("click", doInspect);
$("#store-url").addEventListener("keydown", (e) => { if (e.key === "Enter") doInspect(); });
$("#detail-close").addEventListener("click", closeDetail);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#img-viewer").classList.contains("hidden")) { closeViewer(); return; }
  if (!$("#pack-view").classList.contains("hidden")) { closePack(); return; }
  if (!$("#detail").classList.contains("hidden")) closeDetail();
});
$("#detail-prev").addEventListener("click", () => {
  if (state.preview.page > 1) { state.preview.page--; loadDetailFiles(); }
});
$("#detail-next").addEventListener("click", () => {
  if (state.preview.page * state.preview.size < state.preview.total) {
    state.preview.page++; loadDetailFiles();
  }
});

// ================= 下载：小窗 / 后台 / 下载页 =================
const dl = { taskId: "", title: "", timer: 0, done: false, source: "detail" };

$("#detail-download").addEventListener("click", async () => {
  if (!state.preview.url || state.preview.downloading) return;
  const btn = $("#detail-download");
  btn.disabled = true;
  btn.textContent = "下载中……";
  $("#detail-progress-text").classList.add("hidden");

  try {
    const r = await api("/api/download", {
      method: "POST",
      body: JSON.stringify({
        url: state.preview.url,
        type: $("#detail-type").value,
        title: $("#detail-name").textContent.trim(),
      }),
    });
    if (!r.ok) throw new Error(r.message || "发起失败");
    state.preview.downloading = true;
    openDlPop(r.task_id, $("#detail-name").textContent.trim() || "新包");
  } catch (e) {
    const t = $("#detail-progress-text");
    t.classList.remove("hidden");
    t.textContent = "下载失败：" + e.message;
    btn.disabled = false;
    btn.textContent = "下载入库";
    state.preview.downloading = false;
  }
});

function openDlPop(taskId, title, source = "detail") {
  dl.taskId = taskId; dl.title = title; dl.done = false; dl.source = source;
  $("#dl-title").textContent = title || "正在下载";
  $("#dl-fill").style.width = "2%";
  $("#dl-pct").textContent = "0%";
  $("#dl-meta").textContent = "准备中……";
  $("#dl-cancel").disabled = false;
  $("#dl-bg").textContent = "后台下载";
  $("#dl-pop").classList.remove("hidden");
  $("#dl-mini").classList.add("hidden");
  paintDlBadge(1);
  pollDl();
}

async function pollDl() {
  clearTimeout(dl.timer);
  if (!dl.taskId || dl.done) return;
  let s = null;
  try {
    s = await api(`/api/download/status?id=${dl.taskId}`);
  } catch (e) {
    $("#dl-meta").textContent = "查进度失败：" + e.message;
  }
  if (s && !s.done) {
    const pct = calcPct(s);
    $("#dl-fill").style.width = `${Math.max(2, pct)}%`;
    $("#dl-pct").textContent = `${Math.round(pct)}%`;
    $("#dl-meta").textContent = dlLabel(s);
    $("#dl-mini-text").textContent = `下载中 ${Math.round(pct)}%`;
    dl.timer = setTimeout(pollDl, 1200);
    return;
  }
  if (s && s.done) { await finishDl(s); return; }
  dl.timer = setTimeout(pollDl, 2500);   // 查不到就慢点重试，别打爆接口
}

async function finishDl(s) {
  dl.done = true;
  const ok = !!s.success;
  $("#dl-fill").style.width = "100%";
  $("#dl-pct").textContent = ok ? "100%" : "—";
  $("#dl-meta").textContent = s.message || (ok ? "已入库。" : "下载失败。");
  $("#dl-cancel").disabled = true;
  $("#dl-bg").textContent = "关闭";
  $("#dl-mini-text").textContent = ok ? "下载完成" : "下载结束";
  // 收在后台时不自动消失，点开看结果，点「关闭」才收掉

  const btn = $("#detail-download");
  btn.disabled = false;
  btn.textContent = ok ? "已入库" : "下载入库";
  // 进度和结果都在弹窗里看，浮层里不再冒提示文字
  const t = $("#detail-progress-text");
  if (t) { t.classList.add("hidden"); t.textContent = ""; }
  state.preview.downloading = false;
  paintDlBadge(0);

  if (dl.source === "store") {
    if (ok) {
      markStoreCardDone(dl.title);
      toast(`${dl.title} 已入库 · ${s.result?.total ?? ""} 张`);
    } else {
      const card = $$("#store-index .pack-card").find((c) => c.dataset.label === dl.title);
      const b = card && card.querySelector('[data-act="install"]');
      if (b) { b.disabled = false; b.textContent = "下载"; }
      toast(`${dl.title} 下载失败`);
    }
    refreshSideStats();
  }

  if (ok) {
    if (state.preview.summary) state.preview.summary.exists_locally = true;
    await loadPacks();
    await refreshSideStats();
  }
  if (!$("#view-dl").classList.contains("hidden")) loadDownloads();
}

$("#dl-hide").addEventListener("click", () => $("#dl-bg").click());

$("#dl-bg").addEventListener("click", () => {
  if (dl.done) { $("#dl-pop").classList.add("hidden"); return; }
  $("#dl-pop").classList.add("hidden");
  $("#dl-mini").classList.remove("hidden");
});

$("#dl-mini").addEventListener("click", () => {
  $("#dl-mini").classList.add("hidden");
  $("#dl-pop").classList.remove("hidden");
});

$("#dl-cancel").addEventListener("click", async () => {
  if (!dl.taskId || dl.done) return;
  const ok = await askConfirm({
    title: "取消这次下载？",
    text: "已经拉下来的临时文件会删掉，不会留半截包占用空间。",
    okText: "取消下载", danger: true,
  });
  if (!ok) return;
  const taskId = dl.taskId;
  $("#dl-cancel").disabled = true;
  $("#dl-meta").textContent = "正在取消……";
  try {
    await api("/api/download/cancel", {
      method: "POST", body: JSON.stringify({ id: taskId }),
    });
  } catch (e) {
    $("#dl-cancel").disabled = false;
    $("#dl-meta").textContent = "取消失败：" + e.message;
    toast("取消失败：" + e.message);
    return;
  }
  // 确认取消后弹窗直接收掉，胶囊也一起收，不留个「已取消」挂在那儿
  clearTimeout(dl.timer);
  // 卡片上那句「下载中…」得放回来，不然取消完还一直挂着
  const card = $$("#store-index .pack-card").find((c) => c.dataset.label === dl.title);
  const ib = card && card.querySelector('[data-act="install"]');
  if (ib) { ib.disabled = false; ib.textContent = "下载"; }
  dl.done = true;
  dl.taskId = "";
  $("#dl-pop").classList.add("hidden");
  $("#dl-mini").classList.add("hidden");
  $("#dl-meta").textContent = "";
  toast("已取消下载");
  try {
    const t = await api("/api/download/tasks");
    paintDlBadge(t.live || 0);
  } catch {}
  if (!$("#view-dl").classList.contains("hidden")) loadDownloads();
});

function paintDlBadge(n) {
  const b = $("#nav-dl-badge");
  if (!b) return;
  b.textContent = n;
  b.classList.toggle("hidden", !n);
}

// ================= 下载页 =================
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function loadDownloads() {
  const box = $("#dl-list");
  if (!box) return;
  let r;
  try {
    r = await api("/api/download/tasks");
  } catch (e) {
    box.innerHTML = `<p class="msg">读取失败：${esc(e.message)}</p>`;
    return;
  }
  const items = r.items || [];
  $("#dl-count").textContent = `共 ${items.length} 条`;
  paintDlBadge(r.live || 0);

  if (!items.length) {
    box.innerHTML = `<p class="msg">还没有下载记录，去资源广场挑一个包。</p>`;
    return;
  }

  box.innerHTML = items.map((it) => {
    const name = it.title || it.pack_name || it.url || "未命名";
    const pct = it.done ? (it.ok ? 100 : 0) : calcPct(it);
    const stateText = it.done
      ? (it.ok ? "已完成" : (it.cancel ? "已取消" : "失败"))
      : (it.cancel ? "取消中" : "下载中");
    const cls = it.done ? (it.ok ? "ok" : "bad") : "run";
    const sub = [
      it.done_items ? `${it.done_items} 张` : "",
      it.message ? esc(it.message) : "",
      fmtTime(it.created),
    ].filter(Boolean).join(" · ");
    const go = it.ok && it.pack_id
      ? `<button class="btn ghost" data-pack="${it.pack_id}" data-v="open">打开这个包</button>`
      : "";
    const peek = !it.done
      ? `<button class="btn ghost" data-peek="${it.id}" data-name="${esc(name)}">看进度</button>`
      : "";
    const del = it.done
      ? `<button class="btn ghost danger" data-del="${it.id}">删掉</button>`
      : "";
    return `
      <div class="dl-item">
        <div class="dl-item-head">
          <span class="dl-name">${esc(name)}</span>
          <span class="dl-badge ${cls}">${stateText}</span>
          ${go}${peek}${del}
        </div>
        <div class="dl-item-bar"><span style="width:${Math.max(1, pct)}%"></span></div>
        <div class="dl-sub">${sub}</div>
      </div>`;
  }).join("");

  box.querySelectorAll('[data-v="open"]').forEach((b) => {
    b.addEventListener("click", () => {
      switchView("manage");
      openPack(b.dataset.pack);
    });
  });
  box.querySelectorAll("[data-peek]").forEach((b) => {
    b.addEventListener("click", () => openDlPop(b.dataset.peek, b.dataset.name, "dl"));
  });
  box.querySelectorAll("[data-del]").forEach((b) => {
    b.addEventListener("click", () => deleteDlRecord(b.dataset.del));
  });
}

async function deleteDlRecord(id) {
  const ok = await askConfirm({
    title: "删掉这条下载记录？",
    text: "只是清掉这条记录，已经入库的表情包和图都还在。",
    okText: "删掉", danger: true,
  });
  if (!ok) return;
  try {
    const r = await api("/api/download/delete", {
      method: "POST", body: JSON.stringify({ ids: [id] }),
    });
    if (r.skipped) { toast("这个任务还在跑，先取消再删"); return; }
    await loadDownloads();
  } catch (e) {
    toast("删不掉：" + e.message);
  }
}

$("#dl-clear").addEventListener("click", async () => {
  const ok = await askConfirm({
    title: "清空下载记录？",
    text: "把所有已结束的记录清掉，正在下的留着。表情包本身不受影响。",
    okText: "清空", danger: true,
  });
  if (!ok) return;
  try {
    const r = await api("/api/download/clear", { method: "POST", body: "{}" });
    await loadDownloads();
    toast(`清掉 ${r.count} 条记录`);
  } catch (e) {
    toast("清不掉：" + e.message);
  }
});


// ================= 我的表情包 =================
async function loadPacks() {
  state.packs = await api("/api/packs");
  const sel = $("#manage-pack");
  const keep = sel.value || state.packs[0]?.id;
  sel.innerHTML = state.packs.map((p) => `
    <option value="${p.id}">${esc(p.display_name || p.name)} · ${p.image_count} 张${p.enabled ? "（启用）" : ""}</option>
  `).join("") || `<option value="">还没有集合</option>`;
  if (keep && state.packs.some((p) => String(p.id) === String(keep))) sel.value = String(keep);
  state.manage.packId = sel.value;
  paintPackStats();
  renderPackCards();
}

// 页头那两个数：入了几个包、几个开着
function paintPackStats() {
  const all = state.packs.length;
  const on = state.packs.filter((p) => p.enabled).length;
  const t = $("#pack-total");
  const o = $("#pack-on");
  if (t) t.textContent = all;
  if (o) o.textContent = on;
}

// 包卡片：跟资源广场一套样式，多出启用/禁用/删除
function renderPackCards() {
  const box = $("#pack-grid");
  if (!box) return;
  const sel = state.manage.selPacks;
  box.innerHTML = state.packs.map((p) => `
    <div class="pack-card ${String(p.id) === String(state.manage.packId) ? "sel" : ""}
         ${sel.has(String(p.id)) ? "checked" : ""}" data-pack-id="${p.id}">
      <input type="checkbox" class="pack-check" data-pack-id="${p.id}"
             ${sel.has(String(p.id)) ? "checked" : ""} />
      <div class="cover loading"><span class="cover-emoji">🎴</span></div>
      <div class="pack-body">
        <h3 title="${esc(p.display_name || p.name)}">${esc(p.display_name || p.name)}</h3>
        <div class="pack-by">
          <span class="badge ${p.enabled ? "on" : "off"}">${p.enabled ? "已启用" : "未启用"}</span>
          <span class="vbadge ok">${p.image_count} 张</span>
        </div>
        <p class="pack-desc">${esc(p.description || "")}</p>
      </div>
      <div class="pack-foot">
        <button class="btn primary" data-act="open">预览</button>
        ${p.enabled
          ? `<button class="btn warn" data-act="toggle">禁用</button>`
          : `<button class="btn ok" data-act="toggle">启用</button>`}
        <button class="btn ghost danger" data-act="del">删除</button>
      </div>
    </div>`).join("") || `<p class="msg" style="grid-column:1/-1">还没有集合，去资源广场收一个。</p>`;

  $$("#pack-grid .pack-card").forEach((card) => {
    const id = card.dataset.packId;
    card.addEventListener("click", (e) => {
      if (e.target.closest("[data-act]") || e.target.closest(".pack-check")) return;
      openPack(id);
    });
    card.querySelector('[data-act="open"]')
      .addEventListener("click", () => openPack(id));
    card.querySelector('[data-act="toggle"]')
      .addEventListener("click", () => togglePack(id));
    card.querySelector('[data-act="del"]')
      .addEventListener("click", () => deletePacks([id]));
    card.querySelector(".pack-check").addEventListener("change", (e) => {
      if (e.target.checked) sel.add(id);
      else sel.delete(id);
      card.classList.toggle("checked", e.target.checked);
      paintPackSel();
    });
  });

  paintPackSel();

  // 队列只在这里装一次，别再让 pump 自己往里塞，会无限重复请求
  packCoverQueue.length = 0;
  $$("#pack-grid .pack-card").forEach((card) => packCoverQueue.push(card));
  pumpPackCovers();
}

function paintPackSel() {
  const n = state.manage.selPacks.size;
  const tip = $("#pack-sel");
  const btn = $("#pack-del-sel");
  if (tip) tip.textContent = `已选 ${n}`;
  if (btn) btn.disabled = n === 0;
}

$("#pack-del-sel").addEventListener("click", () => {
  const ids = [...state.manage.selPacks];
  if (!ids.length) { toast("先在卡片左上角勾几个包"); return; }
  deletePacks(ids);
});

// 封面：拿这个包里第一张图，跟资源广场一个路子
const packCoverQueue = [];
let packCoverActive = 0;

function pumpPackCovers() {
  while (packCoverActive < 6 && packCoverQueue.length) {
    const card = packCoverQueue.shift();
    packCoverActive++;
    const pid = card.dataset.packId;
    const cover = card.querySelector(".cover");
    if (!cover) { packCoverActive--; continue; }
    const guard = setTimeout(() => cover.classList.remove("loading"), 8000);  // 卡住也别一直闪灰块

    api(`/api/images?pack_id=${pid}&size=1`)
      .then((d) => {
        const first = d.items && d.items[0];
        if (!first) { clearTimeout(guard); cover.classList.remove("loading"); return; }
        const img = new Image();
        img.onload = () => {
          clearTimeout(guard);
          cover.innerHTML = `<img src="/api/img?id=${first.id}" alt="" />`;
          cover.classList.remove("loading");
        };
        img.onerror = () => { clearTimeout(guard); cover.classList.remove("loading"); };
        img.src = `/api/img?id=${first.id}`;
      })
      .catch(() => { clearTimeout(guard); cover.classList.remove("loading"); })
      .finally(() => { packCoverActive--; pumpPackCovers(); });
  }
}

function paintPackHead(pack) {
  if (!pack) return;
  $("#pack-title").textContent = pack.display_name || pack.name;
  const st = $("#pack-state");
  st.textContent = pack.enabled ? "已启用" : "未启用";
  st.className = "badge " + (pack.enabled ? "on" : "off");
  const btn = $("#pack-toggle");
  if (btn) btn.textContent = pack.enabled ? "禁用" : "启用";
}

// 点卡片：整页铺开看这个包，跟资源广场预览一个路子
function openPack(id) {
  state.manage.packId = id;
  state.manage.page = 1;
  $("#manage-pack").value = id;
  const pack = state.packs.find((p) => String(p.id) === String(id));
  paintPackHead(pack);
  $("#pack-count").textContent = pack ? `${pack.image_count} 张` : "";
  $$("#pack-grid .pack-card").forEach((c) =>
    c.classList.toggle("sel", c.dataset.packId === id));
  $("#pack-view").classList.remove("hidden");
  $("#pack-view").scrollTop = 0;
  renderTagRail();
  loadManageGrid();
}

function closePack() {
  $("#pack-view").classList.add("hidden");
}

$("#pack-close").addEventListener("click", closePack);
$("#pack-toggle").addEventListener("click", () => togglePack(state.manage.packId));
$("#pack-del").addEventListener("click", () => deletePacks([state.manage.packId]));

async function togglePack(id) {
  const pack = state.packs.find((p) => String(p.id) === String(id));
  if (!pack) return;
  const names = new Set(state.packs.filter((p) => p.enabled).map((p) => p.name));
  if (names.has(pack.name)) names.delete(pack.name);
  else names.add(pack.name);
  await api("/api/packs/enable", {
    method: "POST", body: JSON.stringify({ names: [...names] }),
  });
  await loadPacks();
  const now = state.packs.find((p) => String(p.id) === String(id));
  paintPackHead(now);
  $("#pack-count").textContent = now ? `${now.image_count} 张` : "";
}

async function deletePacks(ids) {
  const targets = state.packs.filter((p) => ids.some((i) => String(i) === String(p.id)));
  if (!targets.length) return;
  const names = targets.map((p) => p.display_name || p.name).join("、");
  const imgs = targets.reduce((a, p) => a + p.image_count, 0);
  const multi = targets.length > 1;
  const ok = await askConfirm({
    title: multi ? `删掉这 ${targets.length} 个包？` : `删掉「${names}」？`,
    text: `${names}　共 ${imgs} 张图，删完只能重新收录。`,
    okText: multi ? "删掉这些包" : "删掉这个包",
    danger: true,
  });
  if (!ok) return;

  let done = 0;
  for (const p of targets) {
    try {
      await api("/api/packs/delete", {
        method: "POST", body: JSON.stringify({ id: p.id }),
      });
      done++;
    } catch (e) {
      toast(`「${p.display_name || p.name}」删除失败：${e.message}`);
    }
  }
  state.manage.selPacks.clear();
  state.manage.page = 1;
  await loadPacks();
  closePack();
  await refreshSideStats();
  if (done) toast(`已删掉 ${done} 个包`);
}

async function loadManage() {
  await loadPacks();
}

async function renderTagRail() {
  const q = state.manage.packId ? `?pack_id=${state.manage.packId}` : "";
  const c = await api(`/api/tag/counts${q}`);
  const items = [
    { key: "", label: "全部", n: c.__all__ || 0 },
    { key: "uncategorized", label: "未分类", n: c.uncategorized || 0 },
    ...Object.entries(CONST.standard_tags).map(([k, v]) => ({ key: k, label: v, n: c[k] || 0 })),
  ];
  const curKey = state.manage.status === "uncategorized" ? "uncategorized" : state.manage.tag;

  $("#tag-rail").innerHTML = items.map((it) => `
    <button class="rail-item ${curKey === it.key ? "on" : ""}" data-key="${it.key}">
      <span>${esc(it.label)}</span><span class="n">${it.n}</span>
    </button>`).join("");

  $$("#tag-rail .rail-item").forEach((b) =>
    b.addEventListener("click", () => {
      const key = b.dataset.key;
      state.manage.tag = key === "uncategorized" ? "" : key;
      state.manage.status = key === "uncategorized" ? "uncategorized" : "";
      state.manage.page = 1;
      $$("#tag-rail .rail-item").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      loadManageGrid();
    }));
}

async function loadManageGrid() {
  const m = state.manage;
  const q = new URLSearchParams({ page: m.page, size: m.size });
  if (m.packId) q.set("pack_id", m.packId);
  if (m.tag) q.set("tag", m.tag);
  if (m.status) q.set("status", m.status);

  const data = await api(`/api/images?${q}`);
  m.selected.clear();
  updateSelCount();

  $("#manage-count").textContent = `共 ${data.total} 张`;
  $("#manage-grid").innerHTML = data.items.map((it) => manageTile(it)).join("")
    || `<p class="msg">这个分类下还没有图。</p>`;
  $("#manage-pageinfo").textContent =
    `${data.page} / ${Math.max(1, Math.ceil(data.total / m.size))}`;
  $("#select-all").checked = false;

  bindManageTiles();
}

const DOT = {
  confirmed: "dot-confirmed", pending_high: "dot-high",
  pending_low: "dot-low", uncategorized: "dot-uncat",
};

function manageTile(it) {
  const tags = it.tags || [];
  return `
    <div class="tile" data-id="${it.id}">
      <span class="status-dot ${DOT[it.review_status] || "dot-uncat"}"></span>
      <input type="checkbox" class="tile-check" data-id="${it.id}" />
      <button class="tile-del" data-id="${it.id}" title="删掉这张">✕</button>
      <img class="thumb" loading="lazy" src="/api/img?id=${it.id}" alt="" />
      <div class="tile-body">
        <div class="tagline">${tags.map((t) =>
          `<span class="mini-tag">${esc(CONST.standard_tags[t] || t)}</span>`).join("")
          || `<span class="mini-tag" style="color:#8d97a8;background:#181d27">未分类</span>`}</div>
        <div class="filen" title="${esc(it.file_name)}">${esc(it.file_name)}</div>
        <input class="tile-tags-edit" placeholder="＋加标签" value="${tags.join(",")}" />
      </div>
    </div>`;
}

function openViewer(id) {
  const v = $("#img-viewer");
  v.querySelector("img").src = `/api/img?id=${id}`;
  v.classList.remove("hidden");
}

function closeViewer() {
  const v = $("#img-viewer");
  v.classList.add("hidden");
  v.querySelector("img").src = "";
}

$("#img-viewer").addEventListener("click", closeViewer);

function bindManageTiles() {
  $$("#manage-grid .tile").forEach((tile) => {
    const id = Number(tile.dataset.id);
    const cb = tile.querySelector(".tile-check");

    // 点图看大图，勾选交给右上角那个框，不互相抢
    tile.querySelector(".thumb").addEventListener("click", () => openViewer(id));
    cb.addEventListener("change", () => {
      if (cb.checked) state.manage.selected.add(id);
      else state.manage.selected.delete(id);
      tile.classList.toggle("sel", cb.checked);
      updateSelCount();
    });

    // 单张删除：不用勾选，点叉就行
    tile.querySelector(".tile-del").addEventListener("click", async (e) => {
      e.stopPropagation();
      const ok = await askConfirm({
        title: "删掉这一张？",
        text: "先进回收站，想用回来随时能复原。",
        okText: "删掉", danger: true,
      });
      if (!ok) return;
      await api("/api/images/delete", {
        method: "POST", body: JSON.stringify({ ids: [id] }),
      });
      await loadManageGrid();
      await refreshSideStats();
      toast("已放进回收站");
    });

    const input = tile.querySelector(".tile-tags-edit");
    input.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter") return;
      const tags = input.value.split(",").map((s) => s.trim()).filter(Boolean);
      await api("/api/tags", { method: "POST", body: JSON.stringify({ id, tags }) });
      await loadManageGrid();
      await refreshSideStats();
    });
  });
}

function updateSelCount() {
  $("#sel-count").textContent = `已选 ${state.manage.selected.size}`;
}

$("#select-all").addEventListener("change", (e) => {
  $$("#manage-grid .tile").forEach((tile) => {
    const cb = tile.querySelector(".tile-check");
    cb.checked = e.target.checked;
    const id = Number(tile.dataset.id);
    if (e.target.checked) state.manage.selected.add(id);
    else state.manage.selected.delete(id);
    tile.classList.toggle("sel", e.target.checked);
  });
  updateSelCount();
});

$("#batch-add").addEventListener("click", () => batchTags("add"));
$("#batch-set").addEventListener("click", () => batchTags("set"));

async function batchTags(mode) {
  const ids = [...state.manage.selected];
  if (!ids.length) { toast("先在图上勾选要处理的图片"); return; }
  const tag = $("#batch-tag").value;
  const r = await api("/api/tags/batch", {
    method: "POST", body: JSON.stringify({ ids, tags: [tag], mode }),
  });
  await loadManageGrid();
  await refreshSideStats();
  toast(`已处理 ${r.count} 张`);
}

$("#batch-delete").addEventListener("click", async () => {
  const ids = [...state.manage.selected];
  if (!ids.length) { toast("先勾选要删的图"); return; }
  const ok = await askConfirm({
    title: `删掉这 ${ids.length} 张？`,
    text: "先进回收站，随时能复原；回收站最多留 50 张。",
    okText: "删掉", danger: true,
  });
  if (!ok) return;
  const r = await api("/api/images/delete", {
    method: "POST", body: JSON.stringify({ ids }),
  });
  await loadManageGrid();
  await refreshSideStats();
  toast(`已放进回收站 ${r.count} 张`);
});

$("#manage-prev").addEventListener("click", () => {
  if (state.manage.page > 1) { state.manage.page--; loadManageGrid(); }
});
$("#manage-next").addEventListener("click", () => {
  state.manage.page++; loadManageGrid();
});
$("#manage-pack").addEventListener("change", (e) => {
  state.manage.packId = e.target.value;
  state.manage.page = 1;
  renderTagRail();
  loadManageGrid();
});

// ================= 回收站 =================
async function loadTrash() {
  const box = $("#trash-grid");
  const msg = $("#trash-msg");
  msg.textContent = "读取回收站……";
  try {
    const r = await api("/api/trash");
    if (!r.ok) throw new Error(r.message || "读不到");
    msg.textContent = r.total
      ? `共 ${r.total} 张，上限 ${r.max} 张，满了自动清最旧的。`
      : "回收站是空的，删了的图会先落在这儿。";
    box.innerHTML = r.items.map((it) => `
      <div class="pack-card" data-tid="${it.id}">
        <div class="cover trash-cover">
          <img src="/api/trash/img?id=${it.id}" alt="" loading="lazy" />
        </div>
        <div class="pack-body">
          <h3 title="${esc(it.file_name)}">${esc(it.file_name || "未命名")}</h3>
          <div class="pack-by"><span class="vbadge ok">${esc(it.pack_name)}</span></div>
          <p class="pack-desc">${esc(it.deleted_at)} 删的</p>
        </div>
        <div class="pack-foot">
          <button class="btn primary" data-act="restore">复原</button>
          <button class="btn ghost danger" data-act="purge">彻底删</button>
        </div>
      </div>`).join("");

    $$("#trash-grid [data-act]").forEach((b) => {
      const card = b.closest(".pack-card");
      const tid = Number(card.dataset.tid);
      if (b.dataset.act === "restore") {
        b.addEventListener("click", () => restoreTrash([tid]));
      } else {
        b.addEventListener("click", () => purgeTrash([tid]));
      }
    });
  } catch (e) {
    msg.textContent = "回收站读不到：" + e.message;
  }
}

async function restoreTrash(ids) {
  try {
    const r = await api("/api/trash/restore", {
      method: "POST", body: JSON.stringify({ ids }),
    });
    if (r.count) toast(`已复原 ${r.count} 张，回到原来的包`);
    if (r.skipped) toast(`有 ${r.skipped} 张还不了（原来的包已经删了）`);
    await loadTrash();
    await refreshSideStats();
  } catch (e) {
    toast("复原失败：" + e.message);
  }
}

async function purgeTrash(ids) {
  const ok = await askConfirm({
    title: ids.length ? "彻底删掉这几张？" : "清空回收站？",
    text: ids.length
      ? `这 ${ids.length} 张会彻底删除，找不回来。`
      : "回收站里的图会全部彻底删除，找不回来。",
    okText: ids.length ? "彻底删" : "清空",
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await api("/api/trash/purge", {
      method: "POST", body: JSON.stringify({ ids }),
    });
    toast(`已彻底删掉 ${r.count} 张`);
    await loadTrash();
  } catch (e) {
    toast("删除失败：" + e.message);
  }
}

$("#trash-empty").addEventListener("click", () => purgeTrash([]));

// ================= 审核中心 =================
async function loadReview() {
  const [high, low] = await Promise.all([
    api("/api/images?status=pending_high&size=60"),
    api("/api/images?status=pending_low&size=60"),
  ]);
  $("#high-count").textContent = `共 ${high.total} 张`;
  $("#low-count").textContent = `共 ${low.total} 张`;
  $("#grid-high").innerHTML = high.items.map(reviewTile).join("") || emptyHint("没有高置信待确认的图");
  $("#grid-low").innerHTML = low.items.map(reviewTile).join("") || emptyHint("没有待审核的图");
  bindReviewTiles("#grid-high");
  bindReviewTiles("#grid-low");
}

function emptyHint(text) {
  return `<p class="msg" style="grid-column:1/-1">${text}</p>`;
}

function reviewTile(it) {
  const tags = it.tags || [];
  return `
    <div class="tile" data-id="${it.id}" data-status="${it.review_status}">
      <span class="status-dot ${DOT[it.review_status] || "dot-uncat"}"></span>
      <img class="thumb" loading="lazy" src="/api/img?id=${it.id}" alt="" />
      <div class="tile-body">
        <div class="tagline">${tags.map((t) =>
          `<span class="mini-tag">${esc(CONST.standard_tags[t] || t)}</span>`).join("")
          || `<span class="mini-tag" style="color:#8d97a8;background:#181d27">无标签</span>`}</div>
        <input class="tile-tags-edit" value="${tags.join(",")}" placeholder="＋加标签" />
        <div style="display:flex;gap:6px;margin-top:6px">
          <button class="btn primary" style="flex:1;padding:5px 0;font-size:11.5px">入库</button>
          <button class="btn ghost" style="padding:5px 8px;font-size:11.5px">撤回</button>
        </div>
      </div>
    </div>`;
}

function bindReviewTiles(sel) {
  $$(`${sel} .tile`).forEach((tile) => {
    const id = Number(tile.dataset.id);
    const cur = tile.dataset.status;
    const [confirmBtn, revertBtn] = tile.querySelectorAll(".btn");
    const input = tile.querySelector(".tile-tags-edit");

    confirmBtn.addEventListener("click", async () => {
      const tags = input.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (tags.length) {
        await api("/api/tags", { method: "POST", body: JSON.stringify({ id, tags }) });
      }
      await api("/api/review/confirm", { method: "POST", body: JSON.stringify({ id }) });
      loadReview();
      refreshSideStats();
    });

    revertBtn.addEventListener("click", async () => {
      const to = cur === "pending_low" ? "pending_high" : "uncategorized";
      await api("/api/review/revert", { method: "POST", body: JSON.stringify({ id, to }) });
      loadReview();
    });
  });
}

$("#confirm-high").addEventListener("click", async () => {
  const r = await api("/api/review/batch", {
    method: "POST", body: JSON.stringify({ status: "pending_high" }),
  });
  $("#review-msg").textContent = `高置信整档入库 ${r.count} 张`;
  loadReview();
  refreshSideStats();
});

$("#confirm-low").addEventListener("click", async () => {
  const r = await api("/api/review/batch", {
    method: "POST", body: JSON.stringify({ status: "pending_low" }),
  });
  $("#review-msg").textContent = `待审核整档入库 ${r.count} 张`;
  loadReview();
  refreshSideStats();
});

$("#review-local").addEventListener("click", async () => {
  const r = await api("/api/tagging/local", { method: "POST" });
  $("#review-msg").textContent = `本地打标 ${r.processed} 张，还剩 ${r.remaining} 张没处理`;
  loadReview();
  refreshSideStats();
});

$("#review-ai").addEventListener("click", async () => {
  $("#review-msg").textContent = "AI 识别中，稍等……";
  try {
    const r = await api("/api/tagging/ai", { method: "POST", body: JSON.stringify({ limit: 50 }) });
    $("#review-msg").textContent = r.message || "识别完成";
    loadReview();
    refreshSideStats();
  } catch (e) {
    $("#review-msg").textContent = "识别失败：" + e.message;
  }
});

// ================= 概览 =================
async function loadOverview() {
  const ov = await api("/api/overview");
  const packs = await api("/api/packs");
  const total = Object.values(ov.review).reduce((a, b) => a + b, 0);

  const cards = [
    { num: total, label: "总图片", cls: "" },
    { num: ov.review.confirmed || 0, label: "已确认（可用于配图）", cls: "ok" },
    { num: (ov.review.pending_high || 0) + (ov.review.pending_low || 0), label: "待你过一眼", cls: "accent" },
    { num: ov.review.uncategorized || 0, label: "还没分类", cls: "warn" },
  ];
  $("#stat-cards").innerHTML = cards.map((c) => `
    <div class="stat-card ${c.cls}">
      <div class="num">${c.num}</div><div class="label">${c.label}</div>
    </div>`).join("");

  const max = Math.max(1, ...Object.values(ov.tag_stats));
  $("#tag-bars").innerHTML = Object.entries(CONST.standard_tags).map(([k, v]) => {
    const n = ov.tag_stats[k] || 0;
    return `
      <div class="tb-row">
        <span class="tb-name">${v}</span>
        <span class="tb-track"><span class="tb-fill" style="width:${(n / max) * 100}%"></span></span>
        <span class="tb-num">${n}</span>
      </div>`;
  }).join("");

  $("#pack-table").innerHTML = packs.map((p) => `
    <div class="pt-row">
      <span class="pt-name">${esc(p.display_name || p.name)}</span>
      <span class="pt-meta">${p.type === "meme" ? "梗图包" : "情绪包"}</span>
      <span class="pt-meta">${p.image_count} 张</span>
      <span class="badge ${p.enabled ? "on" : "off"}">${p.enabled ? "已启用" : "未启用"}</span>
    </div>`).join("") || `<p class="msg">还没有集合，去资源广场下一个。</p>`;

  await refreshSideStats();
}

// ================= 设置 / 心情映射 =================
async function loadMoods() {
  const data = await api("/api/moods");
  const custom = data.custom || {};
  const builtin = data.builtin || {};

  const entries = Object.entries(custom);
  $("#mood-list").innerHTML = entries.map(([w, t]) => `
    <div class="mood-item">
      <span class="mw" title="${esc(w)}">${esc(w)}</span>
      <span class="mt">${esc(CONST.standard_tags[t] || t)}</span>
      <button data-word="${esc(w)}">✕</button>
    </div>`).join("") || `<p class="msg" style="grid-column:1/-1">还没加过自定义词，下面内置的够用就先不加。</p>`;

  $$("#mood-list button").forEach((b) =>
    b.addEventListener("click", async () => {
      await api("/api/moods/delete", { method: "POST", body: JSON.stringify({ word: b.dataset.word }) });
      loadMoods();
    }));

  $("#builtin-moods").innerHTML = Object.entries(builtin).map(([tag, words]) => `
    <div class="bm-row">
      <span class="bm-tag">${esc(CONST.standard_tags[tag] || tag)}</span>
      <span class="bm-words">${words.map((w) => `<span class="mini-tag">${esc(w)}</span>`).join("")}</span>
    </div>`).join("");
}

$("#mood-add-btn").addEventListener("click", async () => {
  const word = $("#mood-word").value.trim();
  if (!word) return;
  await api("/api/moods", {
    method: "POST", body: JSON.stringify({ mappings: { [word]: $("#mood-tag").value } }),
  });
  $("#mood-word").value = "";
  loadMoods();
});

// ================= 设置 / 预览加速 =================
function paintThumbState(s) {
  const box = $("#thumb-state");
  const btn = $("#thumb-install");
  $("#thumb-max").value = s.max_mb ?? 128;
  $("#thumb-used").textContent =
    `已用 ${s.used_mb ?? 0} MB · ${s.count ?? 0} 张缩略图`;
  if (s.has_pil) {
    box.className = "thumb-state";
    box.innerHTML = `缩略图加速已开启，单张约 <b>40KB</b>。`;
    btn.classList.add("hidden");
  } else {
    box.className = "thumb-state warn";
    box.innerHTML = `还没装 Pillow，预览走原图直传（一张两三兆）。`;
    btn.classList.remove("hidden");
  }
}

async function loadThumbState() {
  try {
    paintThumbState(await api("/api/thumb/status"));
  } catch (e) {
    $("#thumb-state").textContent = "状态读取失败：" + e.message;
  }
}

$("#thumb-install").addEventListener("click", async () => {
  const btn = $("#thumb-install");
  btn.disabled = true;
  btn.textContent = "正在装……";
  $("#thumb-state").className = "thumb-state";
  $("#thumb-state").textContent = "正在从镜像源下载 Pillow，一两分钟，别关页面。";
  try {
    await api("/api/deps/install", { method: "POST", body: "{}" });
  } catch (e) {
    $("#thumb-state").textContent = "装失败：" + e.message;
    btn.disabled = false;
    btn.textContent = "重试";
    return;
  }
  for (let i = 0; i < 240; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    let d;
    try {
      d = await api("/api/deps/status?id=pillow");
    } catch {
      continue;
    }
    if (d.status === "running") {
      const sec = i + 1;
      $("#thumb-state").textContent = `正在装…… 已等 ${sec} 秒`;
      continue;
    }
    btn.disabled = false;
    if (d.status === "done" || d.has_pil) {
      btn.textContent = "一键装 Pillow";
      $("#store-msg").textContent = "Pillow 装好了，预览立刻加速。";
      loadThumbState();
    } else {
      btn.textContent = "重试";
      $("#thumb-state").className = "thumb-state warn";
      $("#thumb-state").textContent = "装失败了：" + (d.log || "未知原因").slice(-120);
    }
    return;
  }
  btn.disabled = false;
  btn.textContent = "重试";
});

$("#thumb-save").addEventListener("click", async () => {
  const mb = Number($("#thumb-max").value);
  try {
    const r = await api("/api/thumb/settings", {
      method: "POST", body: JSON.stringify({ max_mb: mb }),
    });
    if (r.error) throw new Error(r.error);
    paintThumbState(r);
    $("#store-msg").textContent = `缓存上限已设为 ${r.max_mb} MB。`;
  } catch (e) {
    $("#store-msg").textContent = "保存失败：" + e.message;
  }
});

// ================= 启动 =================
(async function init() {
  await loadConstants();
  await refreshSideStats();
  loadStoreIndex();
  loadThumbState();
  resumeDl();          // 上次没关页面/还有任务在跑，接着显示小窗
})();

// 刷新或重新进来时，如果后台还有活儿，把进度接回来
async function resumeDl() {
  let r;
  try {
    r = await api("/api/download/tasks");
  } catch (e) {
    return;
  }
  paintDlBadge(r.live || 0);
  const live = (r.items || []).find((it) => !it.done);
  if (!live) return;
  openDlPop(live.id, live.title || live.pack_name || "正在下载");
  $("#dl-mini").classList.add("hidden");
}

