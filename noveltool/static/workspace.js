"use strict";
// Shared local API boundary. All untrusted content goes through textContent/value.
window.NovelUI = {
  token: "",
  async init() { this.token = (await this.api("/api/session")).csrf_token; },
  async api(path, options = {}) {
    const headers = {"Accept": "application/json", ...(options.headers || {})};
    if (options.method && options.method !== "GET") {
      headers["X-Noveltool-Token"] = this.token;
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, {...options, headers, cache:"no-store", credentials:"same-origin"});
    const data = await response.json();
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map(e=>e.msg).join("；") : data.detail;
      throw new Error(detail || `HTTP ${response.status}`);
    }
    return data;
  },
  tell(text, error = false) {
    const el = document.getElementById("message");
    el.textContent = text; el.className = error ? "message error" : "message";
  },
  node(tag, text, cls) {
    const el = document.createElement(tag); if (text !== undefined) el.textContent = text;
    if (cls) el.className = cls; return el;
  },
  button(text, handler) {
    const el = this.node("button", text);
    el.type = "button"; el.addEventListener("click", () => Promise.resolve().then(handler).catch(e=>this.tell(e.message,true)));
    return el;
  },
};
