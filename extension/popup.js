const API_BASE = "http://127.0.0.1:8788";

async function refreshStatus() {
  const status = document.querySelector("#status");
  try {
    const response = await fetch(`${API_BASE}/api/health`);
    const body = await response.json();
    status.textContent = body.ok ? `已连接，模型 ${body.model}` : "服务异常";
  } catch {
    status.textContent = "本地服务未启动";
  }
}

document.querySelector("#open-dashboard").addEventListener("click", () => {
  chrome.tabs.create({ url: API_BASE });
});

document.querySelector("#start").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) chrome.tabs.sendMessage(tab.id, { type: "BCA_START" });
  window.close();
});

document.querySelector("#stop").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) chrome.tabs.sendMessage(tab.id, { type: "BCA_STOP" });
  window.close();
});

refreshStatus();
