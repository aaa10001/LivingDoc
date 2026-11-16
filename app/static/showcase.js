/* LivingDoc Atlas
   一个不依赖框架的仓库架构总览页：项目 → 模块 → 文档 → 全文。
   数据来自 /api/v1/projects/{id}/overview 与 /api/v1/documents/{id}。 */

const API = "/api/v1";

const TYPES = {
  class_diagram: { label: "类图", short: "CLASS", cls: "class", dot: "class" },
  sequence_diagram: { label: "时序图", short: "FLOW", cls: "sequence", dot: "sequence" },
  api_doc: { label: "API 文档", short: "API", cls: "api", dot: "api" },
};

const RUN_LABEL = {
  succeeded: "成功",
  failed: "失败",
  skipped: "跳过",
  running: "进行中",
  queued: "排队",
  cloning: "克隆",
  diffing: "分析",
  parsing: "解析",
  analyzing: "生成",
  publishing: "发布",
};

const state = {
  projects: [],
  projectId: null,
  overview: null,
  module: null,
  type: "",
  docId: null,
  detail: null,
  loadToken: 0,
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function shortSha(sha) {
  return sha ? String(sha).slice(0, 7) : "—";
}

function compactNumber(value) {
  const n = Number(value || 0);
  if (!n) return "0";
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}m`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatDuration(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 3200);
}

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    headers: { Accept: "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const problem = await response.json();
      message = problem.detail || problem.title || message;
    } catch (_) { /* 非 JSON 错误也保留状态码 */ }
    throw new Error(message);
  }
  return response.json();
}

function setLoading(active) {
  $("loadingBar").hidden = !active;
  document.body.classList.toggle("is-loading", active);
}

function parseHash() {
  return new URLSearchParams(location.hash.replace(/^#/, ""));
}

function syncHash() {
  if (!state.projectId) return;
  const params = new URLSearchParams();
  params.set("project", state.projectId);
  if (state.module) params.set("module", state.module);
  if (state.docId) params.set("doc", state.docId);
  history.replaceState(null, "", `#${params.toString()}`);
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  reveal();
  try {
    const projects = await api("/projects");
    state.projects = projects;
    renderProjectPicker(projects);

    const hash = parseHash();
    const requested = projects.find((p) => p.id === hash.get("project"));
    const project = requested || projects[0];
    if (!project) {
      renderNoProjects();
      return;
    }
    $("projectSelect").value = project.id;
    await loadProject(project.id, {
      module: hash.get("module"),
      type: hash.get("type"),
      doc: hash.get("doc"),
    });
  } catch (error) {
    toast(`加载失败：${error.message}`);
    renderNoProjects("无法连接到 LivingDoc API。确认服务已启动后刷新。");
  }
}

function renderProjectPicker(projects) {
  const select = $("projectSelect");
  select.innerHTML = projects
    .map((project) => `<option value="${esc(project.id)}">${esc(project.name || project.repo_full_name)} · ${esc(project.repo_full_name)}</option>`)
    .join("");
  select.disabled = projects.length <= 1;
}

function reveal() {
  requestAnimationFrame(() => {
    document.querySelectorAll(".reveal").forEach((el) => el.classList.add("in"));
  });
}

/* ------------------------------------------------------------- load state */

async function loadProject(projectId, route = {}) {
  const token = ++state.loadToken;
  setLoading(true);
  try {
    const overview = await api(`/projects/${projectId}/overview`);
    if (token !== state.loadToken) return;
    state.overview = overview;
    state.projectId = projectId;
    state.module = route.module && overview.modules.some((m) => m.key === route.module)
      ? route.module
      : null;
    state.type = route.type && TYPES[route.type] ? route.type : "";
    state.docId = null;
    state.detail = null;
    render();
    syncHash();
    if (route.doc) await openDoc(route.doc);
  } catch (error) {
    toast(`加载项目失败：${error.message}`);
    renderNoProjects("项目数据读取失败。");
  } finally {
    if (token === state.loadToken) setLoading(false);
  }
}

/* ------------------------------------------------------------------ render */

function render() {
  const data = state.overview;
  if (!data) return;

  renderRepo(data);
  renderStats(data);
  renderAtlas(data);
  renderDocs(data);
  renderRuns(data);
}

function renderRepo(data) {
  const project = data.project;
  const repo = data.repo;
  const run = data.last_run;

  $("repoName").textContent = project.repo_full_name || "未命名仓库";
  $("repoName").href = repo.web_url || "#";
  $("repoNavLink").href = repo.web_url || "#";
  $("repoOpen").href = repo.web_url || "#";

  $("repoBranch").textContent = repo.branch || "—";
  $("repoCommit").textContent = shortSha(repo.head_sha);
  $("repoCommit").href = repo.head_url || "#";
  $("repoPath").textContent = repo.docs_path || "—";
  $("repoRun").textContent = run ? (RUN_LABEL[run.status] || run.status) : "暂无";

  const live = $("repoLive");
  const ok = run && run.status === "succeeded";
  live.innerHTML = `<i></i>${ok ? "in sync" : run ? (RUN_LABEL[run.status] || run.status) : "ready"}`;
  live.style.color = ok ? "#a9b3a9" : run && run.status === "failed" ? "#d8a39d" : "#b3a98f";

  const core = $("coreName");
  core.textContent = project.repo_full_name || "—";
  $("coreMeta").textContent = `${data.stats.modules} modules · ${repo.docs_path || ""}`;
  $("coreStatus").textContent = run ? (RUN_LABEL[run.status] || run.status) : "no runs yet";

  $("heroSignals").innerHTML = [
    `<span class="signal"><i></i>${data.stats.documents} documents</span>`,
    `<span class="signal blue"><i></i>${data.stats.modules} modules</span>`,
    data.stats.pending
      ? `<span class="signal brass"><i></i>${data.stats.pending} pending review</span>`
      : `<span class="signal"><i></i>all reviewed</span>`,
    `<span class="signal"><i></i>${esc(repo.branch || "branch")}</span>`,
  ].join("");
}

function renderStats(data) {
  $("statDocs").textContent = data.stats.documents;
  $("statModules").textContent = data.stats.modules;
  $("statPending").textContent = data.stats.pending;
  $("statTokens").textContent = compactNumber(data.stats.tokens_out);
}

function moduleLatest(documents) {
  const versions = documents
    .map((doc) => doc.latest_version || doc.current_version)
    .filter((v) => Number.isFinite(v));
  return versions.length ? Math.max(...versions) : null;
}

function renderAtlas(data) {
  const grid = $("moduleGrid");
  if (!data.modules.length) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><span class="empty-mark">∅</span><h3>还没有模块</h3><p>生成文档后，这里会按 target_key 聚合出模块图谱。</p></div>`;
    return;
  }

  grid.innerHTML = data.modules.map((module, index) => {
    const counts = module.counts || {};
    const total = module.count || module.documents.length;
    const active = state.module === module.key ? " active" : "";
    const bars = ["class", "sequence", "api"]
      .map((type) => {
        const key = type === "class" ? "class_diagram" : type === "sequence" ? "sequence_diagram" : "api_doc";
        return `<span class="${counts[key] ? type : "empty"}"></span>`;
      })
      .join("");
    const latest = moduleLatest(module.documents);
    return `
      <button class="module-card${active}" data-key="${esc(module.key)}" type="button">
        <span class="module-top">
          <span class="module-index">${String(index + 1).padStart(2, "0")}</span>
          <span class="module-count">${total} doc${total === 1 ? "" : "s"}</span>
        </span>
        <h3>${esc(module.label || module.key)}</h3>
        <span class="module-types">
          <span class="type-pill class${counts.class_diagram ? "" : " zero"}"><i></i>${counts.class_diagram || 0}</span>
          <span class="type-pill sequence${counts.sequence_diagram ? "" : " zero"}"><i></i>${counts.sequence_diagram || 0}</span>
          <span class="type-pill api${counts.api_doc ? "" : " zero"}"><i></i>${counts.api_doc || 0}</span>
        </span>
        <span class="module-bar">${bars}</span>
        <span class="module-foot">
          <span>${latest ? `v${latest}` : "—"}</span>
          ${module.pending ? `<span class="pending">${module.pending} pending</span>` : "<span>reviewed</span>"}
        </span>
      </button>`;
  }).join("");
}

function visibleDocuments(data) {
  const docs = data.modules
    .filter((module) => !state.module || module.key === state.module)
    .flatMap((module) => module.documents.map((doc) => ({ ...doc, moduleKey: module.key })));
  return state.type ? docs.filter((doc) => doc.doc_type === state.type) : docs;
}

function statusBadge(doc) {
  if (doc.has_candidate) return `<span class="badge pending">待确认候选</span>`;
  if (doc.status === "stale") return `<span class="badge candidate">候选</span>`;
  if (doc.manual_edited) return `<span class="badge active">人工维护</span>`;
  return `<span class="badge active">已生效</span>`;
}

function renderDocs(data) {
  const docs = visibleDocuments(data);
  document.querySelectorAll("#docFilters .filter").forEach((button) => {
    button.classList.toggle("active", button.dataset.type === state.type);
  });

  const filter = $("moduleFilter");
  if (state.module) {
    filter.hidden = false;
    filter.innerHTML = `<span>筛选模块</span><strong>${esc(state.module)}</strong><button class="clear-filter" type="button" data-clear-module>清除</button>`;
  } else {
    filter.hidden = true;
    filter.innerHTML = "";
  }

  const grid = $("docGrid");
  const empty = $("docsEmpty");
  if (!docs.length) {
    grid.innerHTML = "";
    empty.hidden = false;
    const title = empty.querySelector("h3");
    const text = empty.querySelector("p");
    if (data.modules.length && (state.module || state.type)) {
      title.textContent = "这个筛选下没有文档";
      text.textContent = "换一个类型，或清除模块筛选。";
    } else {
      title.textContent = "这个仓库还没有生成文档";
      text.textContent = "配置 Webhook 后触发一次 push，或先看文档评审页里的演示数据。";
    }
    return;
  }
  empty.hidden = true;

  grid.innerHTML = docs.map((doc) => {
    const meta = TYPES[doc.doc_type] || { label: doc.doc_type, short: doc.doc_type, cls: "" };
    const version = doc.current_version ? `v${doc.current_version}` : "—";
    return `
      <button class="doc-card ${meta.cls}" data-id="${esc(doc.id)}" type="button">
        <span class="doc-card-top">
          <span class="doc-type"><i></i>${esc(meta.short)}</span>
          ${statusBadge(doc)}
        </span>
        <h3>${esc(doc.title)}</h3>
        <p class="doc-preview">${esc(doc.preview || "暂无正文预览。")}</p>
        <span class="doc-card-foot">
          <span>${version} · ${esc(doc.target_key)}</span>
          <span class="doc-card-arrow" aria-hidden="true">↗</span>
        </span>
      </button>`;
  }).join("");
}

function runDuration(run) {
  if (run.stage_detail) {
    try {
      const detail = JSON.parse(run.stage_detail);
      if (detail && Number.isFinite(Number(detail.duration_ms))) return Number(detail.duration_ms);
    } catch (_) { /* stage_detail 也可能是人类可读的一句话 */ }
  }
  if (run.started_at && run.finished_at) {
    return new Date(run.finished_at) - new Date(run.started_at);
  }
  return null;
}

function renderRuns(data) {
  const list = $("runList");
  if (!data.runs.length) {
    list.innerHTML = `<li class="run-row"><span class="run-dot"></span><span class="run-main"><span class="run-title">还没有运行记录</span><span class="run-sub">Webhook 到达后会在这里留下足迹。</span></span><span></span><span></span></li>`;
    return;
  }
  list.innerHTML = data.runs.map((run) => {
    const status = run.status || "unknown";
    const duration = runDuration(run);
    const title = run.trigger_type === "pull_request"
      ? `PR ${run.trigger_ref || ""}`
      : `Push ${run.trigger_ref || ""}`;
    const error = run.error_message ? ` · ${run.error_message}` : "";
    return `
      <li class="run-row">
        <span class="run-dot ${esc(status)}"></span>
        <span class="run-main">
          <span class="run-title"><strong>${esc(title)}</strong><span>${esc(run.head_sha ? shortSha(run.head_sha) : "")}</span></span>
          <span class="run-sub">${esc(run.files_changed ?? 0)} files · ${esc(run.entities_changed ?? 0)} symbols · ${esc(run.tokens_out ?? 0)} tokens${esc(error)}</span>
        </span>
        <span class="run-status">${esc(RUN_LABEL[status] || status)}${duration != null ? ` · ${esc(formatDuration(duration))}` : ""}</span>
        <span class="run-time">${esc(formatTime(run.queued_at || run.started_at))}</span>
      </li>`;
  }).join("");
}

function renderNoProjects(message = "还没有项目。先跑一次 analyze_push 或 seed_demo。") {
  $("repoName").textContent = "暂无仓库";
  $("repoName").removeAttribute("href");
  $("repoBranch").textContent = "—";
  $("repoCommit").textContent = "—";
  $("repoPath").textContent = "—";
  $("repoRun").textContent = "—";
  $("coreName").textContent = "—";
  $("coreMeta").textContent = "0 modules";
  $("coreStatus").textContent = "waiting for a repo";
  $("statDocs").textContent = "0";
  $("statModules").textContent = "0";
  $("statPending").textContent = "0";
  $("statTokens").textContent = "0";
  $("moduleGrid").innerHTML = "";
  $("docGrid").innerHTML = "";
  $("runList").innerHTML = "";
  const empty = $("docsEmpty");
  empty.hidden = false;
  empty.querySelector("h3").textContent = "还没有可展示的仓库";
  empty.querySelector("p").textContent = message;
}

/* ------------------------------------------------------------------ events */

$("projectSelect").addEventListener("change", (event) => {
  const projectId = event.target.value;
  if (projectId) loadProject(projectId);
});

$("moduleGrid").addEventListener("click", (event) => {
  const card = event.target.closest(".module-card");
  if (!card) return;
  const key = card.dataset.key;
  state.module = state.module === key ? null : key;
  state.type = "";
  syncHash();
  renderAtlas(state.overview);
  renderDocs(state.overview);
});

$("docFilters").addEventListener("click", (event) => {
  const button = event.target.closest(".filter");
  if (!button) return;
  state.type = button.dataset.type || "";
  syncHash();
  renderDocs(state.overview);
});

$("moduleFilter").addEventListener("click", (event) => {
  if (!event.target.closest("[data-clear-module]")) return;
  state.module = null;
  syncHash();
  renderAtlas(state.overview);
  renderDocs(state.overview);
});

$("docGrid").addEventListener("click", (event) => {
  const card = event.target.closest(".doc-card");
  if (card) openDoc(card.dataset.id);
});

$("drawerClose").addEventListener("click", closeDrawer);
$("drawerBackdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("drawer").classList.contains("open")) closeDrawer();
});

window.addEventListener("hashchange", () => {
  const hash = parseHash();
  const projectId = hash.get("project");
  if (!projectId) return;
  if (projectId !== state.projectId) {
    loadProject(projectId, { module: hash.get("module"), type: hash.get("type"), doc: hash.get("doc") });
    return;
  }
  const doc = hash.get("doc");
  if (doc && doc !== state.docId) openDoc(doc);
  if (!doc && state.docId) closeDrawer();
});

/* ------------------------------------------------------------------ drawer */

async function openDoc(documentId) {
  if (!documentId) return;
  state.docId = documentId;
  syncHash();
  const drawer = $("drawer");
  const backdrop = $("drawerBackdrop");
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  backdrop.hidden = false;
  requestAnimationFrame(() => backdrop.classList.add("open"));
  document.body.classList.add("drawer-open");

  $("drawerTitle").textContent = "正在读取…";
  $("drawerType").textContent = "Document";
  $("drawerMeta").innerHTML = "";
  $("drawerActions").innerHTML = "";
  $("drawerBody").innerHTML = `<div class="drawer-skeleton"><span class="skeleton-line short"></span><span class="skeleton-line mid"></span><span class="skeleton-block"></span></div>`;

  try {
    const detail = await api(`/documents/${documentId}`);
    if (state.docId !== documentId) return;
    state.detail = detail;
    renderDrawer(detail);
  } catch (error) {
    $("drawerBody").innerHTML = `<p class="drawer-note">读取失败：${esc(error.message)}</p>`;
  }
}

function closeDrawer() {
  state.docId = null;
  state.detail = null;
  syncHash();
  const drawer = $("drawer");
  const backdrop = $("drawerBackdrop");
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  backdrop.classList.remove("open");
  document.body.classList.remove("drawer-open");
  setTimeout(() => { if (!state.docId) backdrop.hidden = true; }, 420);
}

function renderDrawer(detail) {
  const meta = TYPES[detail.doc_type] || { label: detail.doc_type, short: "DOC", cls: "" };
  const current = detail.current || {};
  const version = current.version ? `v${current.version}` : "—";
  $("drawerType").textContent = `${meta.short} · ${version}`;
  $("drawerTitle").textContent = detail.title || detail.target_key || "文档";
  $("drawerMeta").innerHTML = `
    <div><span>Module</span><strong title="${esc(detail.target_key)}">${esc(detail.target_key || "—")}</strong></div>
    <div><span>Status</span><strong>${detail.has_candidate ? "待确认候选" : detail.manual_edited ? "人工维护" : "已生效"}</strong></div>
    <div><span>Updated</span><strong>${esc(formatTime(detail.updated_at))}</strong></div>
    <div><span>Format</span><strong>${esc(detail.content_format || "—")}</strong></div>`;

  $("drawerActions").innerHTML = `
    <a class="action-button primary" href="${esc(detail.review_url || "./index.html")}" target="_blank" rel="noreferrer">在评审页打开 ↗</a>
    <button class="action-button" type="button" data-action="copy">复制源码</button>
    <button class="action-button" type="button" data-action="download">下载</button>`;

  const content = current.content || "";
  if (!content.trim()) {
    $("drawerBody").innerHTML = `<p class="drawer-note">这份文档还没有正文。</p>`;
    return;
  }

  const isPlantUml = (detail.content_format || "").includes("plantuml") || content.trimStart().startsWith("@startuml");
  if (isPlantUml) {
    renderPlantUml(detail, content);
  } else {
    $("drawerBody").innerHTML = `<div class="markdown">${renderMarkdown(content)}</div>`;
  }
}

async function renderPlantUml(detail, content) {
  const body = $("drawerBody");
  body.innerHTML = `<p class="drawer-note">正在用 PlantUML 渲染图…</p>`;
  let rendered = null;
  try {
    const response = await fetch(`${API}/documents/${detail.id}/render`, { method: "POST" });
    if (response.ok) rendered = await response.json();
  } catch (_) { /* 渲染失败时退回源码，不影响阅读 */ }

  let html = "";
  if (rendered && rendered.svg_url) {
    html += `<div class="svg-stage"><img alt="${esc(detail.title)} 的 PlantUML 图" src="${esc(rendered.svg_url)}" /></div>`;
  } else {
    const reason = (rendered && rendered.error) || "当前环境没有可用的 PlantUML 渲染器，已退回源码。";
    html += `<p class="drawer-note">${esc(reason)}</p>`;
  }
  html += `
    <details class="source-details">
      <summary>查看 PlantUML 源码</summary>
      ${codeBlock(content)}
    </details>`;
  body.innerHTML = html;
}

function codeBlock(content) {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const numbers = lines.map((_, index) => index + 1).join("\n");
  return `<div class="code-frame"><div class="line-numbers">${numbers}</div><pre><code>${esc(content)}</code></pre></div>`;
}

$("drawerActions").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button || !state.detail) return;
  const content = (state.detail.current || {}).content || "";
  const action = button.dataset.action;

  if (action === "copy") {
    try {
      await navigator.clipboard.writeText(content);
      toast("源码已复制到剪贴板");
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = content;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      toast("源码已复制");
    }
  }

  if (action === "download") {
    const format = state.detail.content_format || "";
    const extension = format.includes("plantuml") ? "puml" : format.includes("markdown") ? "md" : "txt";
    const filename = `${(state.detail.target_key || "document").replace(/[\\/:*?"<>|]+/g, "-")}-${state.detail.doc_type}-v${(state.detail.current || {}).version || 1}.${extension}`;
    const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 4000);
  }
});

/* ------------------------------------------------------------------ markdown */

function safeHref(url) {
  const clean = String(url || "").trim();
  return /^(https?:\/\/|\/|#|\.\/)/i.test(clean) ? clean : "#";
}

function inlineMarkdown(text) {
  const codes = [];
  let html = esc(text).replace(/`([^`]+)`/g, (_, code) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => {
    return `<a href="${safeHref(url)}" target="_blank" rel="noreferrer">${label}</a>`;
  });
  return html.replace(/\u0000(\d+)\u0000/g, (_, index) => `<code>${codes[Number(index)]}</code>`);
}

function isTableSeparator(line) {
  return /^\s*\|?\s*:?-{2,}[-\s:|]*$/.test(line);
}

function splitTableRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function renderMarkdown(source) {
  const lines = String(source || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (!line.trim()) { index += 1; continue; }

    if (line.trimStart().startsWith("```")) {
      const code = [];
      index += 1;
      while (index < lines.length && !lines[index].trimStart().startsWith("```")) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (line.includes("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      const header = splitTableRow(line);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(splitTableRow(lines[index]));
        index += 1;
      }
      out.push(
        `<table><thead><tr>${header.map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join("")}</tr></thead>` +
        `<tbody>${rows.map((row) => `<tr>${header.map((_, i) => `<td>${inlineMarkdown(row[i] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table>`
      );
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*[-*]\s+/, ""));
        index += 1;
      }
      out.push(`<ul>${items.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ul>`);
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*\d+\.\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*\d+\.\s+/, ""));
        index += 1;
      }
      out.push(`<ol>${items.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ol>`);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quote = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      out.push(`<blockquote>${inlineMarkdown(quote.join(" "))}</blockquote>`);
      continue;
    }

    if (/^\s*(---|\*\*\*)\s*$/.test(line)) {
      out.push("<hr />");
      index += 1;
      continue;
    }

    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !/^(#{1,4}\s|```|\s*[-*]\s+|\s*\d+\.\s+|\s*>)/.test(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    out.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
  }

  return out.join("\n");
}

boot();