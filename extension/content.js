(() => {
  "use strict";

  const BCA_API_BASE = "http://127.0.0.1:8788";
  const BCA_STORE_KEYS = [
    "bcaRunning", "bcaQueue", "bcaProcessing",
    "bcaLastReplyHash", "bcaLastCommandId", "bcaChatCount",
  ];

  /* ── utilities ─────────────────────────────────── */

  function sleep(ms) {
    return new Promise(r => setTimeout(r, ms));
  }

  function rand(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
  }

  function cleanText(value, limit = 4000) {
    return String(value || "").replace(/\s+/g, " ").trim().slice(0, limit);
  }

  function textOf(selector) {
    const node = document.querySelector(selector);
    return cleanText(node?.textContent || "");
  }

  function textIncludesRiskPrompt() {
    const body = cleanText(document.body?.innerText || "", 20000);
    return /(验证码|安全验证|账号异常|风险提示|请完成验证|登录已过期|操作频繁|滑块验证|行为验证)/.test(body);
  }

  function chance(probability) {
    return Math.random() < probability;
  }

  /* ── API ───────────────────────────────────────── */

  async function api(path, options = {}) {
    const resp = await fetch(BCA_API_BASE + path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    if (!resp.ok) {
      const detail = await resp.text();
      throw new Error(detail || "HTTP " + resp.status);
    }
    return resp.json();
  }

  /* ── chrome.storage shim ───────────────────────── */

  function getStore(keys = BCA_STORE_KEYS) {
    if (window.chrome?.storage?.local?.get) {
      return chrome.storage.local.get(keys);
    }
    // fallback
    const out = {};
    for (const key of keys) {
      const raw = window.localStorage.getItem("bca:" + key);
      out[key] = raw ? JSON.parse(raw) : undefined;
    }
    return Promise.resolve(out);
  }

  function setStore(values) {
    if (window.chrome?.storage?.local?.set) {
      return chrome.storage.local.set(values);
    }
    for (const [key, value] of Object.entries(values || {})) {
      window.localStorage.setItem("bca:" + key, JSON.stringify(value));
    }
    return Promise.resolve();
  }

  /* ── React helpers ─────────────────────────────── */

  // Set input value in a way that React registers it
  function reactSetValue(el, value) {
    // Use native setter to bypass React's controlled input
    const nativeSetter =
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value") ||
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    if (nativeSetter?.set) {
      nativeSetter.set.call(el, value);
    } else {
      el.value = value;
    }
    // Dispatch events React listens to
    el.dispatchEvent(new Event("input", { bubbles: true, cancelable: true }));
    el.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
    // Also dispatch composition events for some React versions
    el.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true, data: value }));
    el.dispatchEvent(new CompositionEvent("compositionupdate", { bubbles: true, data: value }));
    el.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: value }));
  }

  // For contenteditable
  function reactSetContentEditable(el, value) {
    el.focus();
    el.textContent = value;
    el.dispatchEvent(new InputEvent("input", { bubbles: true, cancelable: true, inputType: "insertText", data: value }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // Simulate a real click (not just dispatchEvent)
  function simulateClick(el) {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 };
    el.dispatchEvent(new PointerEvent("pointerdown", opts));
    el.dispatchEvent(new PointerEvent("pointerup", opts));
    el.dispatchEvent(new MouseEvent("mousedown", opts));
    el.dispatchEvent(new MouseEvent("mouseup", opts));
    el.dispatchEvent(new MouseEvent("click", opts));
    return true;
  }

  /* ── DOM selectors ─────────────────────────────── */

  function findChatButton() {
    // BOSS直聘 "立即沟通" button — try multiple strategies
    const textPatterns = [
      /^立即沟通$/, /^立即溝通$/, /^沟通$/, /^溝通$/,
      /^开聊$/, /^開聊$/, /^继续沟通$/, /^继续$/,
      /^感兴趣$/, /^感興趣$/, /^我要沟通$/, /^立即开聊$/,
      /^聊一聊$/, /^发消息$/,
    ];

    // Strategy 1: buttons with exact text match
    for (const pattern of textPatterns) {
      const btn = Array.from(document.querySelectorAll("button, a, span[role='button'], div[role='button']"))
        .find(node => {
          const text = cleanText(node.textContent, 80);
          return pattern.test(text) && isVisible(node);
        });
      if (btn) return btn;
    }

    // Strategy 2: BOSS-specific class selectors
    const selSelectors = [
      ".chat-btn", "[class*='chat-btn']", "[class*='chat_btn']",
      "[class*='btn-chat']", "[class*='btn_chat']",
      "[class*='communication']", "[class*='greeting']",
      "button[class*='op']", // BOSS often uses 'op-btn'
    ];
    for (const sel of selSelectors) {
      const el = document.querySelector(sel);
      if (el && isVisible(el)) return el;
    }

    // Strategy 3: any button containing the keywords
    const keywordPatterns = [/沟通/, /开聊/, /聊一聊/];
    for (const pattern of keywordPatterns) {
      const btn = Array.from(document.querySelectorAll("button, a.btn, span.btn"))
        .find(node => pattern.test(node.textContent || "") && isVisible(node));
      if (btn) return btn;
    }

    return null;
  }

  function findChatInput() {
    // BOSS直聘 chat input — prefer contenteditable divs or textareas
    // 1. Look inside chat panels/modals first
    const chatContainers = document.querySelectorAll(
      "[class*='chat-input'], [class*='chatInput'], " +
      "[class*='chat-box'], [class*='chatBox'], " +
      "[class*='input-area'], [class*='inputArea'], " +
      "[class*='message-input'], [class*='messageInput'], " +
      "[class*='send-box'], [class*='sendBox'], " +
      "[class*='reply-area'], [class*='replyArea'], " +
      "[class*='chat-footer'], [class*='chatFooter'], " +
      "[class*='dialog-footer'], [class*='dialogFooter'], " +
      "#chat-input, #chatInput, " +
      ".dialog-footer, .chat-footer"
    );
    for (const container of chatContainers) {
      const input = container.querySelector(
        "textarea, [contenteditable='true'], [role='textbox'], input[type='text']"
      );
      if (input && isVisible(input)) return input;
    }

    // 2. Any contenteditable or textarea visible on page
    const allInputs = Array.from(document.querySelectorAll(
      "textarea, [contenteditable='true'], [role='textbox']"
    ));
    const visible = allInputs.filter(isVisible);
    if (visible.length > 0) return visible[0];

    return null;
  }

  function findSendButton() {
    // Look for send button near the chat input
    const sendPatterns = [/^发送$/, /^Send$/, /^打招呼$/, /^发送招呼$/];
    for (const pattern of sendPatterns) {
      const btn = Array.from(document.querySelectorAll("button, span.btn"))
        .find(node => pattern.test(cleanText(node.textContent, 80)) && isVisible(node));
      if (btn) return btn;
    }

    // Try BOSS-specific selectors
    const selectors = [
      "[class*='send-btn']", "[class*='sendBtn']", "[class*='send_btn']",
      "[class*='btn-send']", "[class*='btn_send']",
      "button[class*='send']", "button[class*='submit']",
      // Icon-only send buttons
      "button svg use[*|href*='send']", "button svg[*|href*='send']",
    ];
    for (const sel of selectors) {
      try {
        const el = document.querySelector(sel);
        if (el) return el.closest("button") || el;
      } catch { /* ignore invalid selectors */ }
    }

    // Last resort: any button with send text
    return Array.from(document.querySelectorAll("button"))
      .find(node => /发送|Send|发送招呼/.test(node.textContent || "") && isVisible(node));
  }

  function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  /* ── Job extraction ────────────────────────────── */

  function extractVisibleJobs() {
    const anchors = Array.from(
      document.querySelectorAll('a[href*="job_detail"], a[href*="/job/"], a[ka*="job"]')
    );
    const jobs = anchors.map(anchor => {
      const card = anchor.closest('[class*="job-card"], [class*="jobCard"], [class*="job-primary"], li') || anchor;
      const rawText = cleanText(card.innerText || anchor.innerText, 3000);
      const title =
        cleanText(card.querySelector?.('[class*="job-name"], [class*="jobName"], [class*="name"], h3')?.textContent, 120) ||
        cleanText(anchor.textContent, 120);
      const salary = cleanText(card.querySelector?.('[class*="salary"], [class*="red"]')?.textContent, 80);
      const company = cleanText(card.querySelector?.('[class*="company-name"], [class*="companyName"], [class*="name"]')?.textContent, 120);
      const city = cleanText(card.querySelector?.('[class*="job-location"], [class*="jobLocation"], [class*="area"]')?.textContent, 40);
      return {
        source_key: anchor.href,
        url: anchor.href,
        title, company, salary, city,
        description: rawText,
        raw: { rawText },
      };
    });

    const seen = new Set();
    return jobs.filter(job => {
      if (!job.url || seen.has(job.url)) return false;
      seen.add(job.url);
      return true;
    });
  }

  function extractCurrentJob() {
    const title =
      textOf('[class*="job-name"]') || textOf('[class*="jobName"]') ||
      textOf('[class*="name"]') || textOf("h1") ||
      (document.title || "").replace(/招聘.*/, "");
    const salary = textOf('[class*="salary"]') || textOf('[class*="job-salary"]');
    const company = textOf('[class*="company-name"]') || textOf('[class*="companyName"]') || textOf('[class*="name"]');
    const city = textOf('[class*="job-location"]') || textOf('[class*="jobLocation"]') || textOf('[class*="area"]');
    const description =
      textOf('[class*="job-sec-text"]') || textOf('[class*="jobSecText"]') ||
      textOf('[class*="job-detail"]') || textOf('[class*="jobDetail"]') ||
      textOf('[class*="detail-content"]') || textOf('[class*="detailContent"]') ||
      cleanText(document.body.innerText, 8000);
    return {
      source_key: location.href.split("?")[0],
      url: location.href,
      title, company, salary, city, description,
      raw: { pageTitle: document.title },
    };
  }

  /* ── Panel UI ──────────────────────────────────── */

  let panel;

  function updateStatus(message) {
    if (!panel) return;
    const el = panel.querySelector(".bca-status");
    if (el) el.textContent = message;
  }

  function injectPanel() {
    if (document.querySelector("#bca-panel")) {
      panel = document.querySelector("#bca-panel");
      return;
    }
    panel = document.createElement("div");
    panel.id = "bca-panel";
    panel.innerHTML = `
      <header>
        <strong>BOSS Assistant</strong>
        <span class="bca-badge" id="bca-badge">待命</span>
        <button class="secondary" data-bca="hide" title="收起">−</button>
      </header>
      <main>
        <div class="bca-status">待命</div>
        <div class="bca-row">
          <button data-bca="sync">📋 同步岗位</button>
          <button data-bca="start">▶ 开始投递</button>
          <button class="secondary" data-bca="stop">⏹ 停止</button>
          <button class="secondary" data-bca="reply">💬 回复</button>
        </div>
        <div class="bca-mini">本地服务：127.0.0.1:8788</div>
      </main>
    `;
    document.body.appendChild(panel);
    panel.addEventListener("click", async e => {
      const action = e.target?.dataset?.bca;
      if (!action) return;
      if (action === "hide") panel.style.display = "none";
      if (action === "sync") { updateStatus("同步中..."); await syncVisibleJobs(); }
      if (action === "start") { updateStatus("启动中..."); await startQueue(); }
      if (action === "stop") { updateStatus("已停止"); await stopQueue(); }
      if (action === "reply") { updateStatus("监听回复..."); await monitorRepliesOnce(); }
    });
  }

  /* ── Sync ──────────────────────────────────────── */

  async function syncVisibleJobs() {
    try {
      const jobs = extractVisibleJobs();
      await setStore({ bcaQueue: jobs.map(j => j.url) });
      updateStatus("已同步 " + jobs.length + " 个岗位");
      await api("/api/events", {
        method: "POST",
        body: JSON.stringify({ type: "jobs_synced", payload: { count: jobs.length } }),
      });
    } catch (err) {
      updateStatus("同步失败: " + cleanText(err.message, 120));
    }
  }

  /* ── Queue ─────────────────────────────────────── */

  async function startQueue() {
    const jobs = extractVisibleJobs();
    const urls = jobs.map(j => j.url);
    const current = await getStore(["bcaQueue"]);
    const queue = urls.length ? urls : (current.bcaQueue || []);
    await setStore({ bcaRunning: true, bcaQueue: queue, bcaProcessing: false, bcaChatCount: 0 });
    updateStatus("队列启动，共 " + queue.length + " 个岗位");
    await processQueue();
  }

  async function stopQueue() {
    await setStore({ bcaRunning: false, bcaProcessing: false });
    updateStatus("已停止");
  }

  function isJobDetailPage() {
    return /job_detail/.test(location.href) || /\/job\//.test(location.href);
  }

  function isJobListPage() {
    return /\/geek\/jobs/.test(location.href) || /\/web\/geek/.test(location.href);
  }

  async function nextFromQueue() {
    const store = await getStore(["bcaQueue"]);
    const queue = store.bcaQueue || [];
    const nextUrl = queue.shift();
    await setStore({ bcaQueue: queue });
    return nextUrl;
  }

  /* ── Wait helpers ──────────────────────────────── */

  async function waitFor(predicate, timeoutMs = 10000, intervalMs = 300) {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      const value = predicate();
      if (value) return value;
      await sleep(intervalMs);
    }
    return null;
  }

  async function waitForElement(selector, timeoutMs = 8000) {
    return waitFor(() => {
      const el = document.querySelector(selector);
      return el && isVisible(el) ? el : null;
    }, timeoutMs);
  }

  function waitForChatModal(timeoutMs = 12000) {
    // After clicking "立即沟通", BOSS opens a chat modal/dialog
    // Watch for: new chat container, dialog appearing, or input becoming visible
    return new Promise(resolve => {
      const startTime = Date.now();
      let resolved = false;
      let observer = null;

      function check() {
        if (resolved) return;
        const input = findChatInput();
        if (input) {
          resolved = true;
          if (observer) observer.disconnect();
          resolve(input);
          return;
        }
        if (Date.now() - startTime > timeoutMs) {
          resolved = true;
          if (observer) observer.disconnect();
          resolve(null);
        }
      }

      // Check immediately
      check();
      if (resolved) return;

      // Watch for DOM changes
      observer = new MutationObserver(() => {
        check();
      });
      observer.observe(document.body, { childList: true, subtree: true, attributes: true });

      // Fallback polling
      const interval = setInterval(() => {
        check();
        if (resolved) clearInterval(interval);
      }, 500);
    });
  }

  /* ── Core: process a single job ────────────────── */

  async function processCurrentJob() {
    if (textIncludesRiskPrompt()) {
      await stopQueue();
      updateStatus("⚠ 检测到验证/风险提示，已停止");
      return "risk";
    }

    // Check quota
    let settings, quota;
    try {
      settings = await api("/api/settings");
      quota = await api("/api/automation/quota");
    } catch (err) {
      updateStatus("获取设置失败: " + cleanText(err.message, 80));
      return "error";
    }

    if (!quota.allowed) {
      await stopQueue();
      updateStatus("今日开聊额度已用完: " + quota.used + "/" + quota.limit);
      return "quota_exceeded";
    }

    // Extract job info
    const job = extractCurrentJob();
    updateStatus("评估: " + (job.title || "当前岗位"));

    // Evaluate job
    let evaluation;
    try {
      evaluation = await api("/api/jobs/evaluate", {
        method: "POST",
        body: JSON.stringify({ job }),
      });
    } catch (err) {
      updateStatus("评估失败: " + cleanText(err.message, 80));
      await goNextAfterCooldown(settings);
      return "eval_error";
    }

    const result = evaluation.evaluation || {};
    const score = Number(result.score || 0);
    const minScore = Number(settings.min_score_to_chat || 72);

    if (result.decision === "skip" || score < minScore) {
      await api("/api/events", {
        method: "POST",
        body: JSON.stringify({ type: "job_skipped", payload: { source_key: job.source_key, score } }),
      });
      updateStatus("跳过: " + score + "分");
      await goNextAfterCooldown(settings);
      return "skipped";
    }

    // Find and click "立即沟通" button
    updateStatus("寻找开聊按钮...");
    const chatButton = findChatButton();

    if (!chatButton) {
      await api("/api/events", {
        method: "POST",
        body: JSON.stringify({ type: "chat_button_missing", payload: { source_key: job.source_key } }),
      });
      updateStatus("没找到开聊按钮，跳过");
      await goNextAfterCooldown(settings);
      return "no_button";
    }

    // Click the button
    if (!simulateClick(chatButton)) {
      chatButton.click();
    }
    updateStatus("已点击开聊，等待对话框...");

    // Wait for chat modal/input to appear
    const input = await waitForChatModal(12000);
    if (!input) {
      await api("/api/events", {
        method: "POST",
        body: JSON.stringify({ type: "chat_input_missing", payload: { source_key: job.source_key } }),
      });
      updateStatus("没找到输入框，跳过");
      await goNextAfterCooldown(settings);
      return "no_input";
    }

    // Human-like pause before typing
    await sleep(rand(800, 2000));

    // Get message
    const message =
      result.initial_message ||
      (await api("/api/messages/initial", { method: "POST", body: JSON.stringify({ job }) })).message;

    if (!message) {
      updateStatus("无法生成话术，跳过");
      await goNextAfterCooldown(settings);
      return "no_message";
    }

    // Fill in the message
    if (input.isContentEditable || input.getAttribute("contenteditable") === "true") {
      reactSetContentEditable(input, message);
    } else {
      reactSetValue(input, message);
    }

    updateStatus("已填入话术");

    // Human-like pause
    await sleep(rand(600, 1500));

    if (settings.auto_send_initial) {
      // Find and click send button
      await sleep(rand(400, 1000));
      const sendBtn = findSendButton();
      let sent = false;
      if (sendBtn) {
        sent = simulateClick(sendBtn);
        if (!sent) sendBtn.click();
      }
      // Fallback: try Enter key
      if (!sent && input) {
        input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
        input.dispatchEvent(new KeyboardEvent("keypress", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
        input.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
        sent = true;
      }

      await api("/api/events", {
        method: "POST",
        body: JSON.stringify({
          type: sent ? "chat_started" : "send_button_missing",
          payload: { source_key: job.source_key, score, message: message.slice(0, 200) },
        }),
      });
      updateStatus(sent ? "已发送: " + job.title : "已填入但未找到发送按钮");

      // Update chat count
      const store = await getStore(["bcaChatCount"]);
      const count = (store.bcaChatCount || 0) + 1;
      await setStore({ bcaChatCount: count });
    } else {
      await api("/api/events", {
        method: "POST",
        body: JSON.stringify({
          type: "message_filled",
          payload: { source_key: job.source_key, score, message: message.slice(0, 200) },
        }),
      });
      updateStatus("已填入话术，等待手动发送");
    }

    // Extra wait after sending to let BOSS process
    await sleep(rand(1000, 2500));
    await goNextAfterCooldown(settings);
    return "sent";
  }

  async function goNextAfterCooldown(settings) {
    const minMs = Number(settings.cooldown_min_ms || 9000);
    const maxMs = Number(settings.cooldown_max_ms || 18000);
    const delay = rand(Math.max(minMs, 4000), Math.max(maxMs, minMs + 3000));
    await sleep(delay);

    const store = await getStore(["bcaRunning"]);
    if (!store.bcaRunning) return;

    const nextUrl = await nextFromQueue();
    if (nextUrl) {
      updateStatus("跳转到下一个岗位...");
      // Use location.href for full page navigation
      // This ensures Page.addScriptToEvaluateOnNewDocument fires
      location.href = nextUrl;
    } else {
      await stopQueue();
      updateStatus("✅ 队列完成");
    }
  }

  /* ── Process queue entry ───────────────────────── */

  async function processQueue() {
    const store = await getStore(["bcaRunning", "bcaProcessing"]);
    if (!store.bcaRunning || store.bcaProcessing) return;

    await setStore({ bcaProcessing: true });
    try {
      if (isJobDetailPage()) {
        await processCurrentJob();
        return; // processCurrentJob handles navigation
      }

      // On job list page: navigate to first job in queue
      if (isJobListPage()) {
        const nextUrl = await nextFromQueue();
        if (nextUrl) {
          updateStatus("前往第一个岗位...");
          location.href = nextUrl;
        } else {
          await stopQueue();
          updateStatus("队列为空，请先同步岗位");
        }
        return;
      }

      // On other pages — try to navigate to job
      const nextUrl = await nextFromQueue();
      if (nextUrl) {
        location.href = nextUrl;
      } else {
        await stopQueue();
        updateStatus("队列为空或不在BOSS页面");
      }
    } catch (error) {
      updateStatus("异常: " + cleanText(error.message, 160));
      try {
        await api("/api/events", {
          method: "POST",
          body: JSON.stringify({ type: "automation_error", payload: { message: error.message } }),
        });
      } catch {}
    } finally {
      await setStore({ bcaProcessing: false });
    }
  }

  /* ── Reply monitoring ──────────────────────────── */

  function collectConversationMessages() {
    // Try to find chat messages in BOSS chat UI
    const msgNodes = Array.from(document.querySelectorAll(
      "[class*='message'], [class*='chat-item'], [class*='chatItem'], " +
      "[class*='dialog-item'], [class*='dialogItem'], [class*='bubble'], " +
      "[class*='msg-item'], [class*='msgItem']"
    ));
    return msgNodes
      .map(node => cleanText(node.textContent, 500))
      .filter(text => text.length > 1 && text.length < 500)
      .slice(-10)
      .map((content, i, list) => ({
        role: i === list.length - 1 ? "boss" : "user",
        content,
      }));
  }

  async function monitorRepliesOnce() {
    try {
      const settings = await api("/api/settings");
      if (!settings.auto_reply) return;

      const messages = collectConversationMessages();
      if (!messages.length) return;

      const latest = messages[messages.length - 1]?.content || "";
      const hash = location.href + ":" + latest;
      const store = await getStore(["bcaLastReplyHash"]);
      if (!latest || store.bcaLastReplyHash === hash) return;
      await setStore({ bcaLastReplyHash: hash });

      const job = extractCurrentJob();
      const result = await api("/api/messages/reply", {
        method: "POST",
        body: JSON.stringify({ job, messages }),
      });

      if (result.need_human || result.action !== "reply") {
        updateStatus("需要人工: " + (result.reason || result.action));
        return;
      }

      const input = findChatInput();
      if (!input) {
        updateStatus("生成回复但无输入框");
        return;
      }

      if (input.isContentEditable || input.getAttribute("contenteditable") === "true") {
        reactSetContentEditable(input, result.message);
      } else {
        reactSetValue(input, result.message);
      }

      await sleep(rand(500, 1200));
      const sendBtn = findSendButton();
      let sent = false;
      if (sendBtn) {
        sent = simulateClick(sendBtn);
        if (!sent) sendBtn.click();
      }
      updateStatus(sent ? "已自动回复" : "已填入回复，未发送");
    } catch {
      // Silent fail for monitor
    }
  }

  /* ── Command polling ───────────────────────────── */

  async function pollAutomationCommand() {
    try {
      const store = await getStore(["bcaLastCommandId", "bcaRunning", "bcaQueue"]);
      const result = await api("/api/automation/poll", {
        method: "POST",
        body: JSON.stringify({
          last_command_id: store.bcaLastCommandId || null,
          url: location.href,
          status: textIncludesRiskPrompt() ? "risk_prompt" : "online",
          running: Boolean(store.bcaRunning),
          queue_count: (store.bcaQueue || []).length,
        }),
      });

      const command = result.command;
      if (!command?.id || !command.action) return;

      await setStore({ bcaLastCommandId: command.id });
      updateStatus("命令: " + command.action);

      if (command.action === "run") {
        await syncVisibleJobs();
        await sleep(500);
        await startQueue();
      }
      if (command.action === "sync") await syncVisibleJobs();
      if (command.action === "start") await startQueue();
      if (command.action === "stop") await stopQueue();
      if (command.action === "reply") await monitorRepliesOnce();
      if (command.action === "reset") {
        await stopQueue();
        await setStore({ bcaQueue: [], bcaLastCommandId: null, bcaLastReplyHash: null, bcaChatCount: 0 });
        updateStatus("状态已重置");
      }
    } catch (error) {
      updateStatus("连接异常: " + cleanText(error.message, 120));
    }
  }

  /* ── Extension message listener ────────────────── */

  if (window.chrome?.runtime?.onMessage?.addListener) {
    chrome.runtime.onMessage.addListener(message => {
      if (message?.type === "BCA_START") startQueue();
      if (message?.type === "BCA_STOP") stopQueue();
    });
  }

  /* ── Initialization ────────────────────────────── */

  injectPanel();

  // Resume running queue if was running before navigation
  getStore(["bcaRunning"]).then(store => {
    if (store.bcaRunning) {
      updateStatus("恢复运行中...");
      setTimeout(() => processQueue(), 1200);
    }
  });

  // Periodic queue check
  setInterval(() => {
    getStore(["bcaRunning"]).then(store => {
      if (store.bcaRunning && !textIncludesRiskPrompt()) {
        processQueue();
      }
    });
  }, 5000);

  // Periodic reply monitoring
  setInterval(() => {
    monitorRepliesOnce().catch(() => {});
  }, 15000);

  // Periodic command polling
  setInterval(() => {
    pollAutomationCommand().catch(() => {});
  }, 3000);

  // Initial poll
  setTimeout(() => {
    pollAutomationCommand().catch(() => {});
  }, 1000);

  // Update badge to show we're active
  const badge = document.querySelector("#bca-badge");
  if (badge) badge.textContent = "就绪";
})();
