"use strict";

// Chat history, action tokens and the synthetic session stay in page memory only.
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { sessionId: null, busy: false, lastEvent: null, retry: null, events: [], templatesLoaded: false, templatesLoading: false };
  const text = (value, max = 5000) => typeof value === "string" ? value.slice(0, max) : "";
  const node = (tag, content, className) => {
    const element = document.createElement(tag);
    if (content !== undefined) element.textContent = String(content);
    if (className) element.className = className;
    return element;
  };

  async function api(path, body, method = "POST") {
    let response;
    try {
      response = await fetch("/api/v1/line/simulator/" + path, {
        method, credentials: "same-origin", cache: "no-store",
        headers: { "Accept": "application/json", ...(method === "POST" ? { "Content-Type": "application/json" } : {}) },
        body: method === "POST" ? JSON.stringify(body || {}) : undefined,
      });
    } catch { throw { status: 0 }; }
    let envelope;
    try { envelope = await response.json(); } catch { throw { status: response.status || 500 }; }
    if (!response.ok) throw { status: response.status, code: envelope.error?.code };
    if (!envelope.data || typeof envelope.data !== "object") throw { status: 500 };
    return envelope.data;
  }

  function errorText(error) {
    if (error.status === 403) return "本機模擬器目前無法使用，請確認服務以開發模式啟動並從本機開啟。";
    if ([404, 410].includes(error.status)) return "會話不存在或已過期。請按「建立新會話」重新開始。";
    if (error.status === 422) return "這次操作的格式不正確。輸入仍保留，請修改後再試。";
    if (error.status === 429) return "操作較頻繁，請稍候再試。輸入與對話仍保留。";
    return "目前無法完成這次操作，沒有收到成功確認。輸入與對話仍保留，可重試。";
  }

  function setBusy(value) {
    state.busy = value;
    document.querySelectorAll("button").forEach((button) => {
      if (button.id === "retry-templates") { button.disabled = state.templatesLoading; return; }
      button.disabled = value || (!state.sessionId && !["reset-session", "retry-request"].includes(button.id));
    });
    $("repeat-event").disabled = value || !state.sessionId || !state.lastEvent;
    $("connection-state").textContent = value ? "處理中" : state.sessionId ? "本機會話" : "尚未連線";
    $("chat-history").setAttribute("aria-busy", String(value));
    $("send-message").textContent = value ? "處理中" : "傳送";
  }

  function clearError() { $("request-error").hidden = true; state.retry = null; }
  function showError(error, retry) {
    $("request-error-text").textContent = errorText(error);
    $("request-error").hidden = false;
    state.retry = retry;
    $("retry-request").hidden = !retry;
  }

  function scrollChat() { const log = $("chat-history"); log.scrollTop = log.scrollHeight; }
  function addSystem(message) { $("chat-history").append(node("p", message, "system-message")); scrollChat(); }

  function addUser(label) {
    const item = node("article", undefined, "chat-message user");
    const meta = node("span", "送出中", "message-meta");
    item.append(node("span", "測試使用者", "message-label"), node("div", text(label, 300), "message-bubble"), meta);
    $("chat-history").append(item); scrollChat();
    return meta;
  }

  function safeLink(value) {
    if (typeof value !== "string" || !value || value.length > 2048) return null;
    try {
      const url = new URL(value, window.location.origin);
      if (url.username || url.password || !["http:", "https:"].includes(url.protocol)) return null;
      if (url.origin !== window.location.origin && url.protocol !== "https:") return null;
      return url.href;
    } catch { return null; }
  }

  function renderAction(action, className = "quick-reply", interactive = true) {
    if (!action || typeof action !== "object") return null;
    const label = text(action.label || action.displayText || action.text, 100) || "選擇";
    if (!interactive) return node("span", label, className + " preview-action");
    if (action.type === "uri") {
      const href = safeLink(action.uri);
      if (!href) return node("span", label + "（連結無法開啟）", "muted");
      const link = node("a", label + " ↗", className);
      link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer";
      link.setAttribute("aria-label", label + "（另開分頁）");
      return link;
    }
    if (!["postback", "message"].includes(action.type)) return null;
    const value = action.type === "postback" ? text(action.data, 1000) : text(action.text, 300);
    if (!value) return null;
    const button = node("button", label, className); button.type = "button";
    button.disabled = state.busy || !state.sessionId;
    button.addEventListener("click", () => sendEvent(action.type === "postback" ? { kind: "postback", data: value } : { kind: "text", text: value }, label));
    return button;
  }

  function flexPart(component, budget = { remaining: 300 }, depth = 0, interactive = true) {
    if (!component || typeof component !== "object" || depth > 12 || budget.remaining-- <= 0) return null;
    const children = (parent, items) => (Array.isArray(items) ? items.slice(0, 60) : []).forEach((item) => {
      const rendered = flexPart(item, budget, depth + 1, interactive); if (rendered) parent.append(rendered);
    });
    if (component.type === "bubble") {
      const bubble = node("div", undefined, "flex-bubble");
      ["header", "hero", "body", "footer"].forEach((part) => {
        const rendered = flexPart(component[part], budget, depth + 1, interactive);
        if (rendered) { const section = node("div", undefined, "flex-part"); section.append(rendered); bubble.append(section); }
      });
      return bubble;
    }
    if (component.type === "carousel") { const carousel = node("div", undefined, "flex-carousel"); children(carousel, component.contents); return carousel; }
    if (component.type === "box") {
      const box = node("div", undefined, "flex-box" + (["horizontal", "baseline"].includes(component.layout) ? " horizontal" : ""));
      children(box, component.contents); return box;
    }
    if (component.type === "text") {
      const content = typeof component.text === "string" ? component.text : (Array.isArray(component.contents) ? component.contents.map((span) => text(span?.text)).join("") : "");
      const paragraph = node("p", text(content), "flex-text");
      if (component.weight === "bold") paragraph.classList.add("bold");
      if (["xs", "sm", "md", "lg", "xl", "xxl"].includes(component.size)) paragraph.classList.add("size-" + component.size);
      return paragraph;
    }
    if (component.type === "button") return renderAction(component.action, "flex-action" + (component.style === "primary" ? " filled" : ""), interactive);
    if (component.type === "separator") return node("hr", undefined, "flex-separator");
    if (component.type === "spacer" || component.type === "filler") { const spacer = node("div", undefined, "flex-spacer"); spacer.setAttribute("aria-hidden", "true"); return spacer; }
    return null;
  }

  function addMessages(messages, target = $("chat-history"), preview = false) {
    (Array.isArray(messages) ? messages.slice(0, 10) : []).forEach((message) => {
      if (!message || typeof message !== "object") return;
      const item = node("article", undefined, "chat-message bot");
      item.append(node("span", preview ? "DEMO · 合成結果展示" : "青年補助服務 · 假發送器", "message-label"));
      if (message.type === "flex") {
        item.append(node("p", text(message.altText, 400) || "服務訊息卡", "message-alt"));
        const content = flexPart(message.contents, { remaining: 300 }, 0, !preview);
        if (content) item.append(content);
      } else if (message.type === "text") item.append(node("div", text(message.text), "message-bubble"));
      else item.append(node("div", text(message.altText, 400) || "目前未提供此訊息類型的預覽。", "message-bubble"));
      const quick = node("div", undefined, "quick-replies");
      const choices = Array.isArray(message.quickReply?.items) ? message.quickReply.items.slice(0, 13) : [];
      choices.forEach((choice) => { const button = renderAction(choice.action, "quick-reply", !preview); if (button) quick.append(button); });
      if (quick.childElementCount) item.append(quick);
      target.append(item);
    });
    if (target === $("chat-history")) scrollChat();
  }

  async function loadTemplates() {
    if (state.templatesLoaded || state.templatesLoading) return;
    state.templatesLoading = true;
    $("retry-templates").hidden = true; $("retry-templates").disabled = true;
    $("template-status").textContent = "正在載入三種合成結果卡…";
    $("template-cards").setAttribute("aria-busy", "true");
    try {
      const data = await api("templates", undefined, "GET");
      if (data.mode !== "demo" || !Array.isArray(data.cards) || data.cards.length !== 3 || data.cards.some((card) => !card || !Array.isArray(card.messages))) throw { status: 500 };
      const cards = $("template-cards"); cards.replaceChildren();
      data.cards.forEach((card, index) => {
        const section = node("section", undefined, "template-card");
        section.append(node("h3", text(card.label, 100) || "合成結果卡 " + (index + 1)));
        addMessages(card.messages, section, true); cards.append(section);
      });
      $("template-status").textContent = text(data.notice, 500) || "以下只供比較示範結果，不代表個人資格或補助核定。";
      state.templatesLoaded = true;
    } catch {
      $("template-status").textContent = "目前無法載入合成結果卡。聊天仍可使用，請稍後重試。";
      $("retry-templates").hidden = false;
    } finally {
      state.templatesLoading = false; $("retry-templates").disabled = false;
      $("template-cards").setAttribute("aria-busy", "false");
    }
  }

  function renderMenu(menu) {
    const container = $("rich-menu"); container.replaceChildren(); container.classList.remove("image-unavailable");
    const width = Number(menu?.size?.width); const height = Number(menu?.size?.height);
    const areas = Array.isArray(menu?.areas) ? menu.areas : [];
    if (!(width > 0 && height > 0 && width <= 10000 && height <= 10000) || !areas.length) {
      container.classList.add("image-unavailable"); container.append(node("p", "選單目前無法載入，仍可使用文字指令。", "menu-placeholder"));
      container.setAttribute("aria-busy", "false"); return;
    }
    container.style.aspectRatio = width + " / " + height;
    try {
      const imageUrl = new URL(menu.image_url || "/line-simulator-assets/menu.png", window.location.origin);
      if (imageUrl.origin !== window.location.origin || !imageUrl.pathname.startsWith("/line-simulator-assets/")) throw new Error("Invalid image");
      const image = node("img"); image.alt = "青年補助服務六格選單"; image.width = width; image.height = height;
      image.addEventListener("error", () => { image.hidden = true; container.classList.add("image-unavailable"); });
      image.src = imageUrl.href; container.append(image);
    } catch { container.classList.add("image-unavailable"); }
    areas.slice(0, 20).forEach((area) => {
      const bounds = area.bounds || {}; const values = [bounds.x, bounds.y, bounds.width, bounds.height].map(Number);
      const [x, y, w, h] = values;
      if (!values.every(Number.isFinite) || x < 0 || y < 0 || w <= 0 || h <= 0 || x + w > width || y + h > height) return;
      const action = renderAction(area.action, "menu-hitbox");
      if (!action || action.tagName !== "BUTTON") return;
      const label = text(area.action?.label, 100) || "服務選單";
      action.replaceChildren(node("span", label, "menu-hit-label")); action.setAttribute("aria-label", label);
      action.style.left = x / width * 100 + "%"; action.style.top = y / height * 100 + "%";
      action.style.width = w / width * 100 + "%"; action.style.height = h / height * 100 + "%";
      container.append(action);
    });
    container.setAttribute("aria-busy", "false");
  }

  function recordStatus(data, kind) {
    const revision = Number.isInteger(data.state?.revision) ? data.state.revision : "—";
    const step = ["string", "number"].includes(typeof data.state?.step) ? String(data.state.step).slice(0, 40) : "—";
    const duplicate = data.duplicate === true;
    const result = duplicate ? "重複事件 · 未重複回覆" : kind === "advance" ? "已前進 25 分鐘" : "已由假發送器處理";
    $("event-status").textContent = result + " · 版本 " + revision + " · 步驟 " + step;
    state.events.unshift({ result, revision, step, eventId: text(data.event_id, 12), status: text(data.state?.status, 40), time: new Intl.DateTimeFormat("zh-TW", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date()) });
    state.events = state.events.slice(0, 8);
    const log = $("event-log"); log.replaceChildren();
    state.events.forEach((entry) => {
      const row = node("div", undefined, "event-row"); row.append(node("strong", entry.time + " · " + entry.result));
      row.append(node("span", "版本 " + entry.revision + " · 步驟 " + entry.step + (entry.status ? " · " + entry.status : "") + (entry.eventId ? " · 事件 " + entry.eventId : "")));
      log.append(row);
    });
  }

  async function createSession() {
    if (state.busy) return;
    clearError(); setBusy(true);
    try {
      const data = await api("sessions", {});
      if (typeof data.session_id !== "string" || !data.session_id) throw { status: 500 };
      state.sessionId = data.session_id; state.lastEvent = null; state.events = [];
      $("chat-history").replaceChildren(); $("event-log").replaceChildren(node("p", "還沒有事件。", "muted"));
      $("event-status").textContent = "新會話已建立 · 尚未送出事件";
      $("composer-input").value = "";
      addSystem("本機會話已建立。先點選「補助預檢」，或使用「預檢」文字指令。");
      if (data.notice) addSystem(text(data.notice, 500));
      const welcome = Array.isArray(data.messages) ? data.messages : Array.isArray(data.welcome) ? data.welcome : Array.isArray(data.welcome?.messages) ? data.welcome.messages : data.welcome?.type ? [data.welcome] : [];
      addMessages(welcome); renderMenu(data.rich_menu);
    } catch (error) {
      $("session-loading")?.remove();
      if (!state.sessionId) addSystem("尚未建立會話，請重試連線。");
      showError(error, createSession);
    } finally { setBusy(false); }
  }

  async function sendEvent(event, label, replay = false) {
    if (state.busy || !state.sessionId) return;
    clearError(); setBusy(true);
    const userMeta = addUser(replay ? "重送上一個事件" : label);
    const payload = { session_id: state.sessionId, ...event, ...(replay ? { repeat_last: true } : {}) };
    try {
      const data = await api("events", payload);
      if (data.delivery && data.delivery !== "fake") throw { status: 500 };
      state.lastEvent = { ...event }; userMeta.textContent = data.duplicate ? "重複事件，未重複發送" : "本機事件已處理";
      addMessages(data.messages); recordStatus(data, "event");
      if (event.kind === "text" && $("composer-input").value.trim() === event.text) $("composer-input").value = "";
      if (data.duplicate && !(data.messages || []).length) addSystem("這個事件已處理，沒有再次產生回覆。可繼續點選對話中的選項。");
    } catch (error) {
      userMeta.textContent = "未收到成功確認";
      showError(error, () => sendEvent(event, label, replay));
    } finally { setBusy(false); scrollChat(); }
  }

  async function advanceTime() {
    if (state.busy || !state.sessionId) return;
    clearError(); setBusy(true);
    try {
      const data = await api("advance", { session_id: state.sessionId, minutes: 25 });
      recordStatus(data, "advance");
      addSystem("已模擬對話經過 25 分鐘。現在可點選上方舊按鈕，觀察逾時回覆；也可重新開始預檢。");
    } catch (error) { showError(error, advanceTime); }
    finally { setBusy(false); }
  }

  $("composer").addEventListener("submit", (event) => {
    event.preventDefault(); const value = $("composer-input").value.trim();
    if (value) sendEvent({ kind: "text", text: value }, value);
  });
  document.querySelectorAll("[data-command]").forEach((button) => button.addEventListener("click", () => sendEvent({ kind: "text", text: button.dataset.command }, button.dataset.command)));
  $("view-result").addEventListener("click", () => sendEvent({ kind: "postback", data: "youth:result" }, "查看結果"));
  $("send-attachment").addEventListener("click", () => sendEvent({ kind: "attachment" }, "[合成附件事件：未選檔、未上傳]"));
  $("simulate-failure").addEventListener("click", () => sendEvent({ kind: "failure", text: "預檢" }, "[合成事件：預檢服務暫時失敗]"));
  $("repeat-event").addEventListener("click", () => { if (state.lastEvent) sendEvent(state.lastEvent, "重送上一個事件", true); });
  $("expire-session").addEventListener("click", advanceTime);
  $("reset-session").addEventListener("click", createSession);
  $("retry-request").addEventListener("click", () => { const retry = state.retry; if (retry && !state.busy) retry(); });
  $("template-gallery").addEventListener("toggle", () => { if ($("template-gallery").open) loadTemplates(); });
  $("retry-templates").addEventListener("click", loadTemplates);
  createSession();
})();
