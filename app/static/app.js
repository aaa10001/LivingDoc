/* LivingDoc 评审界面 —— 原生 JS，无构建链。
 * 设计原则：
 *   1) 只认 /api/v1 的 JSON；错误统一是 RFC7807 problem+json。
 *   2) 不用 innerHTML 拼未转义文本：所有来自仓库/模型的内容先 escape。
 *   3) 状态很少，直接用一个 state 对象 + 全量重绘，够用且好调试。
 */
"use strict";

const API = "/api/v1";

const state = {
  meta: null,
  projects: [],
  projectId: null,
  docs: [],
  docId: null,
  doc: null,
  docType: "",
  query: "",
  tab: "current",
  revisions: [],
  viewRevision: null,
  editing: false,
  editText: "",
  editSummary: "",
  editForce: false,
  editDiagnostics: null,
  editError: null,
  render: null,
  renderedFor: null,
  renderLoading: false,
  diff: null,
  diffFrom: "",
  diffTo: "",
  diffError: null,
  publishResults: null,
  publishError: null,
  accepts: null,
};

const el = (id) => document.getElementById(id);
const stage = () => el("stage");

class ApiError extends Error {
  constructor(problem, status) {
    super(problem?.title || problem?.detail || `HTTP ${status}`);
    this.problem = problem || {};
    this.status = status;
  }
}

async function api(path, options = {}) {
  const init = { headers: { Accept: "application/json" }, ...options };
  if (init.body !== undefined && typeof init.body !== "string") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(API + path, init);
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { detail: text }; }
  }
  if (!response.ok) throw new ApiError(data, response.status);
  return data;
}

/* ------------------------------------------------------------------ */
/* 文本工具                                                            */
/* ------------------------------------------------------------------ */
function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function shortSha(sha) {
  return sha ? String(sha).slice(0, 7) : "—";
}

function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatBytes(n) {
  if (!n && n !== 0) return "—";
  if (n < 1024) return `${n} B`;
  return `${(n / 1024).toFixed(1)} KB`;
}

function toast(message, kind = "", ms = 4200) {
  const node = el("toast");
  node.textContent = message;
  node.className = `toast ${kind}`;
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.hidden = true; }, ms);
}

function inlineMarkdown(text) {
  return text
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

function renderMarkdown(source) {
  const lines = esc(source).split(/\r?\n/);
  const out = [];
  let inCode = false;
  let codeLang = "";
  let code = [];
  let list = null; // "ul" | "ol"
  let table = [];

  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((row) => !/^\s*\|?[\s:|-]+\|?\s*$/.test(row));
    if (rows.length) {
      out.push("<table><thead><tr>");
      rows[0].split("|").slice(1, -1).forEach((cell) => out.push(`<th>${inlineMarkdown(cell.trim())}</th>`));
      out.push("</tr></thead><tbody>");
      rows.slice(1).forEach((row) => {
        out.push("<tr>");
        row.split("|").slice(1, -1).forEach((cell) => out.push(`<td>${inlineMarkdown(cell.trim())}</td>`));
        out.push("</tr>");
      });
      out.push("</tbody></table>");
    }
    table = [];
  };

  for (const raw of lines) {
    const line = raw;
    if (line.trim().startsWith("```")) {
      if (inCode) { out.push(`<pre><code>${code.join("\n")}</code></pre>`); inCode = false; code = []; codeLang = ""; }
      else { closeList(); flushTable(); inCode = true; codeLang = line.trim().slice(3); }
      continue;
    }
    if (inCode) { code.push(line); continue; }

    if (/^\s*\|.*\|\s*$/.test(line)) { closeList(); table.push(line); continue; }
    flushTable();

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) { closeList(); out.push(`<h${heading[1].length}>${inlineMarkdown(heading[2])}</h${heading[1].length}>`); continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s-]*$/.test(line)) { closeList(); out.push("<hr />"); continue; }
    if (/^\s*>\s?/.test(line)) { closeList(); out.push(`<blockquote>${inlineMarkdown(line.replace(/^\s*>\s?/, ""))}</blockquote>`); continue; }

    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      const wanted = ul ? "ul" : "ol";
      if (list !== wanted) { closeList(); list = wanted; out.push(`<${wanted}>`); }
      out.push(`<li>${inlineMarkdown((ul || ol)[1])}</li>`);
      continue;
    }
    closeList();
    if (!line.trim()) { out.push(""); continue; }
    out.push(`<p>${inlineMarkdown(line)}</p>`);
  }
  if (inCode) out.push(`<pre><code>${code.join("\n")}</code></pre>`);
  closeList();
  flushTable();
  return out.join("\n");
}

/* ------------------------------------------------------------------ */
/* 启动 / 项目                                                         */
/* ------------------------------------------------------------------ */
async function boot() {
  try {
    state.meta = await api("/meta");
    el("metaLine").textContent =
      `${state.meta.env || "local"} · API v${state.meta.version} · 投递目标：${(state.meta.publish_targets || []).join(", ") || "未配置"}`;
    await loadProjects();
    routeFromHash();
  } catch (error) {
    el("metaLine").textContent = "无法连接后端";
    stage().innerHTML = `<div class="empty"><h2>连接失败</h2><p>${esc(error.message)}</p>
      <p class="mt">确认 uvicorn 已启动，然后刷新页面。</p></div>`;
  }
}

async function loadProjects() {
  state.projects = await api("/projects");
  const select = el("projectSelect");
  select.innerHTML = "";
  for (const project of state.projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = `${project.name || project.repo_full_name} (${project.document_count})`;
    select.appendChild(option);
  }
  el("sidebarHint").textContent = state.projects.length
    ? `${state.projects.length} 个项目`
    : "还没有项目——先向 /webhook/github 推一次事件。";
}

function routeFromHash() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  const projectId = params.get("project");
  const documentId = params.get("doc");

  const known = state.projects.some((p) => p.id === projectId);
  const targetProject = known ? projectId : state.projects[0]?.id;
  if (!targetProject) {
    stage().innerHTML = `<div class="empty"><h2>还没有可评审的项目</h2>
      <p>配置 GitHub Webhook 后推送一次代码，LivingDoc 会在这里生成文档。</p></div>`;
    return;
  }
  if (state.projectId !== targetProject) {
    state.projectId = targetProject;
    el("projectSelect").value = targetProject;
    state.docId = null;
    state.doc = null;
    loadDocs().then(() => { if (documentId) loadDocument(documentId); });
  } else if (documentId && documentId !== state.docId) {
    loadDocument(documentId);
  }
}

async function selectProject(projectId) {
  state.projectId = projectId;
  state.docId = null;
  state.doc = null;
  state.render = null;
  state.renderedFor = null;
  history.replaceState(null, "", `#project=${encodeURIComponent(projectId)}`);
  await loadDocs();
  renderStage();
}

async function loadDocs() {
  if (!state.projectId) return;
  const params = new URLSearchParams();
  if (state.docType) params.set("doc_type", state.docType);
  if (state.query) params.set("q", state.query);
  const query = params.toString();
  state.docs = await api(`/projects/${state.projectId}/documents${query ? `?${query}` : ""}`);
  renderDocList();
}

function renderDocList() {
  const list = el("docList");
  list.innerHTML = "";
  const groups = [
    ["class_diagram", "类图"],
    ["sequence_diagram", "时序图"],
    ["api_doc", "API 文档"],
  ];
  let shown = 0;
  for (const [type, label] of groups) {
    const docs = state.docs.filter((doc) => doc.doc_type === type);
    if (!docs.length) continue;
    const title = document.createElement("div");
    title.className = "doc-group";
    title.textContent = `${label} · ${docs.length}`;
    list.appendChild(title);
    for (const doc of docs) {
      shown += 1;
      const button = document.createElement("button");
      button.type = "button";
      button.className = `doc-item${doc.id === state.docId ? " active" : ""}`;
      button.dataset.docId = doc.id;
      const flags = [];
      if (doc.manual_edited) flags.push("已人改");
      if (doc.has_candidate) flags.push("待确认");
      if (doc.status === "stale") flags.push("过期");
      button.innerHTML = `
        <span class="doc-title">${esc(doc.title || doc.target_key)}</span>
        <span class="doc-key">${esc(doc.target_key)} · v${esc(doc.current_version ?? "—")}</span>
        ${flags.length ? `<span class="badges">${flags.map((f) => `<span class="badge ${f === "待确认" ? "warn" : "info"}">${f}</span>`).join("")}</span>` : ""}`;
      button.addEventListener("click", () => loadDocument(doc.id));
      list.appendChild(button);
    }
  }
  if (!shown) {
    list.innerHTML = `<p class="hint">${state.docs.length ? "该类型下没有文档。" : "还没有文档，等一次分析完成。"}</p>`;
  }
}

/* ------------------------------------------------------------------ */
/* 文档详情                                                            */
/* ------------------------------------------------------------------ */
async function loadDocument(documentId) {
  try {
    state.doc = await api(`/documents/${encodeURIComponent(documentId)}`);
    state.docId = documentId;
    state.tab = "current";
    state.viewRevision = null;
    state.diff = null;
    state.editing = false;
    state.editError = null;
    state.editDiagnostics = null;
    state.render = null;
    state.renderedFor = null;
    state.publishResults = null;
    state.publishError = null;
    state.revisions = [];
    const params = new URLSearchParams(location.hash.replace(/^#/, ""));
    params.set("project", state.doc.project_id);
    params.set("doc", documentId);
    history.replaceState(null, "", `#${params.toString()}`);
    renderDocList();
    renderStage();
    if (state.doc.content_format === "plantuml") checkRender({ silent: true });
  } catch (error) {
    toast(`打开文档失败：${error.message}`, "error");
  }
}

function badgeForStatus(status) {
  if (status === "published" || status === "accepted") return "ok";
  if (status === "stale" || status === "needs_review") return "warn";
  if (status === "failed" || status === "rejected") return "err";
  return "info";
}

function renderStage() {
  const node = stage();
  const doc = state.doc;
  if (!doc) {
    node.innerHTML = `<div class="empty"><h2>选择一份文档开始评审</h2>
      <p>左侧列出仓库里已生成的类图、时序图与 API 文档。人工改过的文档会显示「已人改」，
        AI 的新版本会停在「待确认候选」，需要你显式接受才会生效。</p></div>`;
    return;
  }

  const current = doc.current || {};
  const flags = [
    `<span class="badge ${badgeForStatus(doc.status)}">${esc(doc.status || "unknown")}</span>`,
    `<span class="badge">${esc(doc.content_format || "plantuml")}</span>`,
    doc.manual_edited ? `<span class="badge info">已人改</span>` : "",
    doc.has_candidate ? `<span class="badge warn">AI 候选 v${esc(doc.candidate?.version)}</span>` : "",
    current.render_ok === false ? `<span class="badge err">上次渲染失败</span>` : "",
  ].filter(Boolean).join("");

  const banner = doc.has_candidate
    ? `<div class="banner">
         <div><strong>AI 生成了新版本 v${esc(doc.candidate?.version)}，尚未生效</strong>
         <span class="muted">${doc.manual_edited
           ? "这份文档被人改过，AI 的修改只能作为候选。接受后会覆盖当前正文（旧版本仍可在历史里找回）。"
           : "AI 的修改先作为候选停放，接受后才成为当前版本。"}</span></div>
         <button class="btn primary" data-action="accept-ai">接受候选</button>
       </div>`
    : "";

  const publishedBanner = state.publishResults
    ? `<div class="banner ok"><div><strong>投递完成</strong>
        <span class="muted">${state.publishResults.map((r) => `${esc(r.target)}: ${esc(r.status)}`).join(" · ")}</span></div></div>`
    : "";

  node.innerHTML = `
    <div class="doc-head">
      <h2>${esc(doc.title || doc.target_key)}</h2>
      <div class="keyline">${esc(doc.doc_type)} / ${esc(doc.target_key)}</div>
      <div class="badges">${flags}</div>
    </div>
    ${banner}
    ${publishedBanner}
    <div class="toolbar">
      <button class="btn primary" data-action="edit">✏️ 人工编辑</button>
      <button class="btn" data-action="render-check">${state.renderLoading ? '<span class="spin"></span> 渲染中…' : "🔍 渲染检查"}</button>
      <span class="spacer"></span>
      <span class="muted">当前 v${esc(doc.current_version ?? "—")}${doc.latest && doc.latest.version !== doc.current_version ? ` · 最新 v${esc(doc.latest.version)}` : ""}</span>
    </div>
    <div class="tabs">
      ${tabButton("current", "当前版本")}
      ${tabButton("candidate", `AI 候选${doc.has_candidate ? " ●" : ""}`, !doc.has_candidate)}
      ${tabButton("history", "历史版本")}
      ${tabButton("diff", "差异对比")}
    </div>
    <div class="panel" id="panel">${renderPanel()}</div>`;

  bindStage();
}

function tabButton(key, label, disabled = false) {
  return `<button class="tab${state.tab === key ? " active" : ""}" data-tab="${key}" ${disabled ? "disabled" : ""}>${label}</button>`;
}

function renderPanel() {
  if (state.editing) return renderEditor();
  if (state.tab === "candidate") return renderCandidatePanel();
  if (state.tab === "history") return renderHistoryPanel();
  if (state.tab === "diff") return renderDiffPanel();
  return renderCurrentPanel();
}

function metaChips(doc) {
  const current = doc.current || {};
  return `<div class="meta-grid">
    <div class="meta-cell"><span>当前版本</span><b>v${esc(current.version ?? "—")}</b></div>
    <div class="meta-cell"><span>格式</span><b>${esc(doc.content_format || "—")}</b></div>
    <div class="meta-cell"><span>最近更新</span><b>${esc(fmtTime(doc.updated_at))}</b></div>
    <div class="meta-cell"><span>内容大小</span><b>${esc(formatBytes(current.content_length))}</b></div>
    <div class="meta-cell"><span>模型 / Prompt</span><b>${esc(current.model_name || "—")} · ${esc(current.prompt_version || "—")}</b></div>
    <div class="meta-cell"><span>改动说明</span><b>${esc(current.change_summary || "—")}</b></div>
  </div>`;
}

function renderDiagnostics(items) {
  if (!items || !items.length) return "";
  return `<ul class="diag-list">${items.map((d) =>
    `<li class="diag ${esc(d.severity || "error")}"><div><b>${esc(d.code)}</b> ${esc(d.message)}
      ${d.line ? `<span class="muted">（第 ${esc(d.line)} 行）</span>` : ""}
      ${d.hint ? `<div class="muted">${esc(d.hint)}</div>` : ""}</div></li>`).join("")}</ul>`;
}

function renderCurrentPanel() {
  const doc = state.doc;
  const current = doc.current || {};
  if (!current.content) {
    return `<div class="empty"><h2>当前版本没有正文</h2><p>可能生成失败，等待下一次运行或手工补一份。</p></div>`;
  }

  const source = `<pre class="code light" id="sourceBlock">${esc(current.content)}</pre>`;
  let body;
  if (doc.content_format === "markdown") {
    body = `<div class="markdown">${renderMarkdown(current.content)}</div>
      <details class="mt"><summary class="muted">查看 Markdown 源码</summary>${source}</details>`;
  } else {
    body = renderPlantumlBody();
  }

  const targets = state.meta?.publish_targets || [];
  const publishBox = `<div class="publish-row">
      <strong>投递</strong>
      <select id="publishTarget">
        <option value="">按配置的全部目标</option>
        ${targets.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("")}
      </select>
      <button class="btn" data-action="publish">发布到知识库</button>
      <span class="muted">${targets.length ? "" : "未配置任何投递目标"}</span>
    </div>
    ${state.publishError ? `<div class="result failed">${esc(state.publishError)}</div>` : ""}
    ${state.publishResults ? `<div class="result-list">${state.publishResults.map((r) =>
      `<div class="result ${esc(r.status)}"><b>${esc(r.target)}</b> · ${esc(r.status)}
        ${r.external_url ? ` · <a href="${esc(r.external_url)}" target="_blank" rel="noreferrer">打开</a>` : ""}
        ${r.error ? `<div class="muted">${esc(r.error)}</div>` : ""}</div>`).join("")}</div>` : ""}`;

  return `${metaChips(doc)}
    <div class="panel-head"><h3>文档内容</h3>
      <span class="muted">${current.render_ok === false ? `上次渲染：${esc(current.render_error || "失败")}` : ""}</span>
    </div>
    ${body}
    ${publishBox}`;
}

function renderPlantumlBody() {
  const doc = state.doc;
  const render = state.render;
  if (state.renderLoading) {
    return `<div class="empty"><span class="spin"></span> 正在调用 PlantUML 编译…</div>`;
  }
  if (render && render.svg_url) {
    return `<div class="svg-wrap"><img src="${esc(render.svg_url)}" alt="${esc(doc.title)}" /></div>
      <details class="mt"><summary class="muted">查看 PlantUML 源码</summary>
      <pre class="code light">${esc(doc.current?.content || "")}</pre></details>`;
  }
  const note = render?.error
    ? `<div class="banner"><div><strong>暂时只做了静态校验</strong>
        <span class="muted">${esc(render.error)}</span></div></div>`
    : `<p class="hint">图表可以在此预览；本地没有 PlantUML jar 时会自动降级为静态校验。</p>`;
  return `${note}
    <pre class="code">${esc(doc.current?.content || "")}</pre>
    ${render ? renderDiagnostics(render.diagnostics) : ""}`;
}

function renderCandidatePanel() {
  const doc = state.doc;
  const candidate = doc.candidate;
  if (!candidate) {
    return `<div class="empty"><h2>没有待确认的候选</h2><p>AI 下一次改动会在这里出现。</p></div>`;
  }
  return `<div class="banner"><div><strong>候选 v${esc(candidate.version)}</strong>
      <span class="muted">生成于 ${esc(fmtTime(candidate.created_at))} · ${esc(candidate.model_name || "—")}
      · ${esc(candidate.change_summary || "无说明")}</span></div>
      <button class="btn primary" data-action="accept-ai">接受为当前版本</button></div>
    <div class="panel-head mt"><h3>候选内容</h3>
      <button class="btn small" data-action="candidate-diff">与当前版本比较</button></div>
    ${doc.content_format === "markdown"
      ? `<div class="markdown">${renderMarkdown(candidate.content || "")}</div>`
      : `<pre class="code">${esc(candidate.content || "")}</pre>`}`;
}

function renderHistoryPanel() {
  if (!state.revisions.length) return `<p class="hint">正在加载版本…</p>`;
  const rows = state.revisions.map((rev) => `
    <div class="rev">
      <div class="v">v${esc(rev.version)}</div>
      <div>
        <div class="who"><b>${esc(rev.created_by)}</b> · ${esc(fmtTime(rev.created_at))} · ${esc(formatBytes(rev.content_length))}</div>
        <div class="summary">${esc(rev.change_summary || "—")}</div>
      </div>
      <div class="row-actions">
        <button class="btn small" data-action="view-revision" data-version="${esc(rev.version)}">查看</button>
        <button class="btn small" data-action="diff-vs-current" data-version="${esc(rev.version)}">对比当前</button>
      </div>
    </div>`).join("");
  const viewer = state.viewRevision
    ? `<div class="panel-head mt"><h3>v${esc(state.viewRevision.version)} 内容</h3>
        <button class="btn small" data-action="close-revision">关闭</button></div>
       <pre class="code light">${esc(state.viewRevision.content || "")}</pre>`
    : "";
  return `<div class="rev-list">${rows}</div>${viewer}`;
}

function renderDiffPanel() {
  const doc = state.doc;
  const versions = state.revisions.map((r) => r.version);
  if (!versions.length) return `<p class="hint">正在加载版本…</p>`;
  const to = state.diffTo || doc.current_version || versions[0];
  const earlier = versions.filter((v) => v < to);
  const from = state.diffFrom || (earlier.length ? earlier[0] : to);
  const options = (selected) => versions
    .map((v) => `<option value="${v}" ${Number(selected) === v ? "selected" : ""}>v${v}</option>`).join("");

  let body = `<p class="hint">选择两个版本，查看行级差异。</p>`;
  if (state.diffError) body = `<div class="result failed">${esc(state.diffError)}</div>`;
  if (state.diff) {
    if (state.diff.identical) {
      body = `<div class="banner ok"><div><strong>两个版本内容完全相同</strong></div></div>`;
    } else {
      const lines = state.diff.unified_diff.split("\n").map((line) => {
        let cls = "";
        if (line.startsWith("+++") || line.startsWith("---")) cls = "head";
        else if (line.startsWith("@@")) cls = "hunk";
        else if (line.startsWith("+")) cls = "add";
        else if (line.startsWith("-")) cls = "del";
        return `<div class="diff-line ${cls}"><span class="ln"></span><span class="tx">${esc(line) || " "}</span></div>`;
      }).join("");
      body = `<div class="panel-head"><h3>v${esc(state.diff.from_version)} → v${esc(state.diff.to_version)}</h3>
        <span class="muted">+${state.diff.added} / −${state.diff.removed}</span></div>
        <div class="diff">${lines}</div>`;
    }
  }

  return `<div class="editor-row">
      <label>从 <select id="diffFrom">${options(from)}</select></label>
      <label>到 <select id="diffTo">${options(to)}</select></label>
      <button class="btn" data-action="diff-refresh">比较</button>
    </div>
    <div class="mt">${body}</div>`;
}

function renderEditor() {
  const doc = state.doc;
  const current = doc.current || {};
  return `<div class="editor">
    <div class="panel-head"><h3>人工编辑 · v${esc(current.version ?? "—")} → 新版本</h3>
      <span class="muted">保存后成为当前版本；AI 的后续修改会转为候选。</span></div>
    <textarea id="editContent" spellcheck="false">${esc(state.editText)}</textarea>
    <div class="editor-row">
      <input id="editSummary" type="text" placeholder="改动说明（可选）" value="${esc(state.editSummary)}" />
      <label class="check"><input id="editForce" type="checkbox" ${state.editForce ? "checked" : ""} />
        校验不通过也强制保存</label>
    </div>
    ${state.editError ? `<div class="result failed">${esc(state.editError)}</div>` : ""}
    ${renderDiagnostics(state.editDiagnostics)}
    <div class="editor-row">
      <button class="btn primary" data-action="save-edit">保存为新版本</button>
      <button class="btn" data-action="cancel-edit">取消</button>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------ */
/* 动作                                                                */
/* ------------------------------------------------------------------ */
function bindStage() {
  stage().querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });
  stage().querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => handleAction(button.dataset.action, button));
  });
  const from = el("diffFrom");
  const to = el("diffTo");
  if (from) from.addEventListener("change", () => { state.diffFrom = Number(from.value); state.diff = null; });
  if (to) to.addEventListener("change", () => { state.diffTo = Number(to.value); state.diff = null; });
  if (el("editContent")) {
    el("editContent").addEventListener("input", (event) => { state.editText = event.target.value; });
    el("editSummary").addEventListener("input", (event) => { state.editSummary = event.target.value; });
    el("editForce").addEventListener("change", (event) => { state.editForce = event.target.checked; });
  }
}

async function switchTab(tab) {
  if (tab === "candidate" && !state.doc?.has_candidate) return;
  state.tab = tab;
  if ((tab === "history" || tab === "diff") && !state.revisions.length) {
    try {
      state.revisions = await api(`/documents/${state.docId}/revisions?limit=100`);
    } catch (error) {
      toast(`加载版本失败：${error.message}`, "error");
    }
  }
  renderStage();
  if (tab === "diff" && !state.diff) await loadDiff();
}

async function loadDiff() {
  const doc = state.doc;
  if (!doc) return;
  const params = new URLSearchParams();
  if (state.diffFrom) params.set("from", state.diffFrom);
  if (state.diffTo) params.set("to", state.diffTo);
  try {
    state.diff = await api(`/documents/${doc.id}/diff?${params.toString()}`);
    state.diffError = null;
    state.diffFrom = state.diff.from_version;
    state.diffTo = state.diff.to_version;
  } catch (error) {
    state.diffError = error.message;
  }
  renderStage();
}

async function checkRender({ silent = false } = {}) {
  const doc = state.doc;
  if (!doc) return;
  const version = doc.current_version;
  const key = `${doc.id}:v${version}`;
  if (state.renderedFor === key && state.render) return;
  state.renderedFor = key;
  state.renderLoading = true;
  if (!silent) renderStage();
  try {
    state.render = await api(`/documents/${doc.id}/render`, { method: "POST" });
  } catch (error) {
    state.render = { render_ok: false, diagnostics: [], error: error.message, svg_url: null };
  }
  state.renderLoading = false;
  if (state.tab === "current") renderStage();
}

async function saveEdit() {
  const text = el("editContent")?.value ?? state.editText;
  const summary = el("editSummary")?.value ?? state.editSummary;
  const force = el("editForce")?.checked ?? state.editForce;
  state.editText = text;
  state.editSummary = summary;
  state.editForce = force;
  state.editError = null;
  state.editDiagnostics = null;
  try {
    const result = await api(`/documents/${state.docId}`, {
      method: "PUT",
      body: { content: text, summary: summary || null, force },
    });
    state.editing = false;
    state.doc = result;
    state.render = null;
    state.renderedFor = null;
    await loadDocument(state.docId);
    toast(result.changed ? "已保存为新的当前版本" : "内容没有变化", "ok");
  } catch (error) {
    const problem = error.problem || {};
    state.editError = problem.detail || error.message;
    state.editDiagnostics = problem.diagnostics || null;
    renderStage();
  }
}

async function acceptCandidate() {
  try {
    state.doc = await api(`/documents/${state.docId}/accept-ai`, { method: "POST" });
    state.revisions = [];
    state.render = null;
    state.renderedFor = null;
    renderStage();
    toast("已接受 AI 候选", "ok");
  } catch (error) {
    toast(`接受失败：${error.message}`, "error");
  }
}

async function publishDocument() {
  const target = el("publishTarget")?.value || null;
  state.publishError = null;
  state.publishResults = null;
  try {
    const result = await api(`/documents/${state.docId}/publish`, {
      method: "POST",
      body: { target, message: null },
    });
    state.publishResults = result.results || [];
    const failed = state.publishResults.filter((r) => r.status === "failed");
    toast(failed.length ? "投递完成，但有目标失败" : "投递完成", failed.length ? "error" : "ok");
  } catch (error) {
    state.publishError = error.message;
    toast(`投递失败：${error.message}`, "error");
  }
  renderStage();
}

async function viewRevision(version) {
  try {
    state.viewRevision = await api(`/documents/${state.docId}/revisions/${version}`);
  } catch (error) {
    toast(`加载失败：${error.message}`, "error");
  }
  renderStage();
}

function handleAction(action, button) {
  switch (action) {
    case "edit":
      state.editing = true;
      state.editText = state.doc?.current?.content || "";
      state.editSummary = "";
      state.editForce = false;
      state.editError = null;
      state.editDiagnostics = null;
      renderStage();
      el("editContent")?.focus();
      break;
    case "cancel-edit":
      state.editing = false;
      renderStage();
      break;
    case "save-edit":
      saveEdit();
      break;
    case "accept-ai":
      acceptCandidate();
      break;
    case "render-check":
      state.render = null;
      state.renderedFor = null;
      checkRender();
      break;
    case "publish":
      publishDocument();
      break;
    case "view-revision":
      viewRevision(Number(button.dataset.version));
      break;
    case "close-revision":
      state.viewRevision = null;
      renderStage();
      break;
    case "diff-vs-current":
      state.diffFrom = Number(button.dataset.version);
      state.diffTo = state.doc.current_version;
      state.tab = "diff";
      state.diff = null;
      if (!state.revisions.length) {
        api(`/documents/${state.docId}/revisions?limit=100`).then((rows) => {
          state.revisions = rows;
          loadDiff();
        });
      } else {
        loadDiff();
      }
      break;
    case "diff-refresh":
      state.diff = null;
      loadDiff();
      break;
    case "candidate-diff":
      state.tab = "diff";
      state.diffFrom = state.doc.current_version;
      state.diffTo = state.doc.candidate?.version;
      state.diff = null;
      if (!state.revisions.length) {
        api(`/documents/${state.docId}/revisions?limit=100`).then((rows) => {
          state.revisions = rows;
          loadDiff();
        });
      } else {
        loadDiff();
      }
      break;
    default:
      break;
  }
}

/* ------------------------------------------------------------------ */
/* 事件绑定                                                            */
/* ------------------------------------------------------------------ */
el("projectSelect").addEventListener("change", (event) => selectProject(event.target.value));

let searchTimer = null;
el("searchInput").addEventListener("input", (event) => {
  state.query = event.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadDocs().catch((error) => toast(error.message, "error")), 220);
});

el("typeFilter").addEventListener("click", (event) => {
  const button = event.target.closest(".chip");
  if (!button) return;
  el("typeFilter").querySelectorAll(".chip").forEach((chip) => chip.classList.remove("active"));
  button.classList.add("active");
  state.docType = button.dataset.type || "";
  loadDocs().catch((error) => toast(error.message, "error"));
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.editing) {
    state.editing = false;
    renderStage();
  }
  if ((event.ctrlKey || event.metaKey) && event.key === "s" && state.editing) {
    event.preventDefault();
    saveEdit();
  }
});

window.addEventListener("hashchange", routeFromHash);

boot();