"use strict";

// The browser edits a form; only an explicit Apply sends those changes to RAM.
// Polling updates status, never input values. All server text uses textContent.
const configForm = document.querySelector("#config-form");
const titleForm = document.querySelector("#title-form");
const message = document.querySelector("#message");
const numericFields = new Set([
  "context_window", "context_safety_ratio", "min_chars", "max_chars", "candidate_count",
  "small_source_chars", "small_output_tokens", "small_max_entities", "structured_output_ceiling", "output_retry_limit", "writer_temperature", "analysis_temperature", "api_timeout_seconds", "autosave_seconds"
]);
let token = "";
let loadedVersion = null;
let configDirty = false;
let titleDirty = false;
let busy = false;

function tell(text, isError = false) {
  message.textContent = text;
  message.className = isError ? "message error" : "message";
}

function formState() {
  document.querySelector("#form-state").textContent = configDirty || titleDirty
    ? "表单有修改：尚未全部提交到内存" : "表单与上次载入/提交的内容一致";
}

async function api(path, options = {}) {
  const headers = {"Accept": "application/json", ...(options.headers || {})};
  if (options.method && options.method !== "GET") headers["X-Noveltool-Token"] = token;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {...options, headers, cache: "no-store", credentials: "same-origin"});
  const payload = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map(x => `${x.loc.join(".")}: ${x.msg}`).join("；")
      : payload.detail || `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

function showStatus(status) {
  document.querySelector("#project-title").textContent = status.title;
  document.querySelector("#database-path").textContent = status.database_path;
  document.querySelector("#save-state").textContent = status.last_save_error
    ? "保存失败 · 内存修改仍保留" : status.dirty ? "待写入磁盘" : "已保存到磁盘";
  document.querySelector("#saved-time").textContent = status.last_save_error
    ? status.last_save_error : `上次写入：${new Date(status.last_saved_at).toLocaleString()}`;
  document.querySelector("#version-state").textContent = `内存 ${status.memory_version} / 磁盘 ${status.saved_version}`;
  document.querySelector("#autosave-state").textContent = `自动保存间隔 ${status.autosave_seconds} 秒 · schema ${status.schema_version}`;
  document.querySelector("#stale-warning").hidden = loadedVersion === null || status.memory_version === loadedVersion;
}

async function loadForm() {
  const data = await api("/api/config");
  for (const [key, value] of Object.entries(data.config)) {
    const input = configForm.elements.namedItem(key);
    if (input.type === "checkbox") input.checked = value;
    else input.value = value;
  }
  loadedVersion = data.memory_version;
  titleForm.elements.namedItem("title").value = data.title;
  configDirty = false;
  titleDirty = false;
  formState();
  showStatus(await api("/api/status"));
}

async function action(work) {
  if (busy || loadedVersion === null) return;
  busy = true;
  for (const button of document.querySelectorAll("button")) button.disabled = true;
  try { await work(); }
  catch (error) { tell(error.message, true); }
  finally {
    busy = false;
    for (const button of document.querySelectorAll("button")) button.disabled = false;
  }
}

configForm.addEventListener("input", () => { configDirty = true; formState(); });
titleForm.addEventListener("input", () => { titleDirty = true; formState(); });

configForm.addEventListener("submit", event => {
  event.preventDefault();
  action(async () => {
    const config = {};
    for (const input of configForm.querySelectorAll("input[name],select[name]")) {
      config[input.name] = input.type === "checkbox" ? input.checked
        : numericFields.has(input.name) ? Number(input.value) : input.value;
    }
    const result = await api("/api/config", {method: "PUT", body: JSON.stringify({
      expected_memory_version: loadedVersion, config
    })});
    loadedVersion = result.memory_version;
    configDirty = false;
    formState();
    showStatus(result);
    tell(result.changed ? "配置已更新到内存。等待自动保存，或点击“立即写入磁盘”。" : "配置没有变化，无需额外写盘。");
  });
});

titleForm.addEventListener("submit", event => {
  event.preventDefault();
  action(async () => {
    const result = await api("/api/project", {method: "PUT", body: JSON.stringify({
      expected_memory_version: loadedVersion, title: titleForm.elements.namedItem("title").value
    })});
    loadedVersion = result.memory_version;
    titleDirty = false;
    formState();
    showStatus(result);
    tell(result.changed ? "名称已更新到内存，尚需保存。" : "名称没有变化。");
  });
});

document.querySelector("#save-now").addEventListener("click", () => action(async () => {
  if (configDirty || titleDirty) throw new Error("请先点击对应表单的“应用/更新到内存”，再写入磁盘。");
  const result = await api("/api/save", {method: "POST"});
  showStatus(result);
  tell(result.written ? "SQLite 事务已成功提交。" : "没有待保存修改；未重复写盘。");
}));

document.querySelector("#reload").addEventListener("click", () => action(async () => {
  if ((configDirty || titleDirty) && !window.confirm("重新载入会丢弃尚未提交到内存的表单修改。继续？")) return;
  await loadForm();
  tell("已从服务器内存重新载入表单。");
}));

window.addEventListener("beforeunload", event => {
  if (configDirty || titleDirty) { event.preventDefault(); event.returnValue = ""; }
});

async function start() {
  try {
    token = (await api("/api/session")).csrf_token;
    await loadForm();
    tell("已载入项目。修改后请先应用到内存，再等待或手动保存。");
    window.setInterval(async () => {
      if (busy) return;
      try { showStatus(await api("/api/status")); }
      catch (error) {
        document.querySelector("#save-state").textContent = "无法确认保存状态";
        tell(`无法读取服务状态：${error.message}。服务若已重启，请刷新页面。`, true);
      }
    }, 2000);
  } catch (error) {
    document.querySelector("#save-state").textContent = "连接失败";
    tell(`加载失败：${error.message}`, true);
  }
}
start();
