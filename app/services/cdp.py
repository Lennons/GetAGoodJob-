from __future__ import annotations

import asyncio
import json
import threading
import urllib.request
from pathlib import Path
from typing import Any

import websockets


def list_cdp_targets(port: int) -> list[dict[str, Any]]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def pick_boss_page(port: int) -> dict[str, Any] | None:
    pages = [target for target in list_cdp_targets(port) if target.get("type") == "page"]
    for page in pages:
        if "zhipin.com" in page.get("url", ""):
            return page
    return pages[0] if pages else None


async def cdp_call(
    websocket: Any, message_id: int, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    await websocket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
    while True:
        message = json.loads(await websocket.recv())
        if message.get("id") == message_id:
            if "error" in message:
                raise RuntimeError(message["error"])
            return message


ANTI_DETECTION_SCRIPT = r"""
(() => {
  // Hide webdriver / automation flags that BOSS直聘 may check
  Object.defineProperty(navigator, 'webdriver', { get: () => false });
  // Overwrite chrome runtime if detected via CDP
  if (!window.chrome) {
    window.chrome = {
      runtime: { onMessage: { addListener() {} }, connect() {}, sendMessage() {} },
      storage: {
        local: {
          async get(keys) {
            const list = Array.isArray(keys) ? keys : Object.keys(keys || {});
            const out = {};
            for (const key of list) {
              const raw = window.localStorage.getItem('bca:' + key);
              out[key] = raw ? JSON.parse(raw) : undefined;
            }
            return out;
          },
          async set(values) {
            for (const [key, value] of Object.entries(values || {})) {
              window.localStorage.setItem('bca:' + key, JSON.stringify(value));
            }
          }
        }
      }
    };
  }
  // Remove CDP detection
  if (window.__REACT_DEVTOOLS_GLOBAL_HOOK__) {
    Object.defineProperty(window, '__REACT_DEVTOOLS_GLOBAL_HOOK__', { value: undefined });
  }
  // Pass chrome.permissions checks
  navigator.permissions.query = (orig => function(query) {
    if (query && query.name === 'clipboard-read') {
      return Promise.resolve({ state: 'prompt', onchange: null });
    }
    return orig.call(this, query);
  })(navigator.permissions.query.bind(navigator.permissions));
})();
"""


def build_runner_script(content_js_path: Path) -> str:
    source = content_js_path.read_text(encoding="utf-8")
    return f"""
(() => {{
  if (window.__BCA_INJECTED__) return;
  window.__BCA_INJECTED__ = true;

  // Ensure chrome.storage.local shim is available before content.js runs
  if (!window.chrome || !window.chrome.storage) {{
    window.chrome = window.chrome || {{}};
    window.chrome.storage = {{
      local: {{
        async get(keys) {{
          const list = Array.isArray(keys) ? keys : Object.keys(keys || {{}});
          const out = {{}};
          for (const key of list) {{
            const raw = window.localStorage.getItem(`bca:${{key}}`);
            out[key] = raw ? JSON.parse(raw) : undefined;
          }}
          return out;
        }},
        async set(values) {{
          for (const [key, value] of Object.entries(values || {{}})) {{
            window.localStorage.setItem(`bca:${{key}}`, JSON.stringify(value));
          }}
        }}
      }}
    }};
  }}
  if (!window.chrome.runtime) {{
    window.chrome.runtime = {{
      onMessage: {{ addListener() {{}} }},
      connect() {{ return {{}}; }},
      sendMessage() {{}}
    }};
  }}

  {source}
}})();
"""


async def inject_runner_async(port: int, content_js_path: Path) -> dict[str, Any]:
    page = pick_boss_page(port)
    if not page:
        raise RuntimeError("没有找到可注入的 Chrome 页面")
    websocket_url = page.get("webSocketDebuggerUrl")
    if not websocket_url:
        raise RuntimeError("Chrome 页面缺少调试连接")

    script = build_runner_script(content_js_path)

    async with websockets.connect(
        websocket_url,
        max_size=8 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=10,
    ) as websocket:
        # Enable Runtime domain
        await cdp_call(websocket, 1, "Runtime.enable")
        # Enable Page domain for navigation events
        await cdp_call(websocket, 2, "Page.enable")

        # Inject anti-detection first
        await cdp_call(websocket, 10, "Page.addScriptToEvaluateOnNewDocument", {"source": ANTI_DETECTION_SCRIPT})
        await cdp_call(websocket, 11, "Runtime.evaluate", {"expression": ANTI_DETECTION_SCRIPT, "returnByValue": True})

        # Inject the main runner script for all future documents
        await cdp_call(websocket, 3, "Page.addScriptToEvaluateOnNewDocument", {"source": script})

        # Also evaluate immediately on current page
        result = await cdp_call(
            websocket,
            4,
            "Runtime.evaluate",
            {"expression": script, "awaitPromise": True, "returnByValue": True},
        )

    return {
        "url": page.get("url", ""),
        "title": page.get("title", ""),
        "result": result.get("result", {}),
    }


def inject_runner(port: int, content_js_path: Path) -> dict[str, Any]:
    """Run async CDP injection safely — handles both sync and async contexts."""
    try:
        # Try getting running loop — if none, we can use asyncio.run directly
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop, safe to use asyncio.run
        return asyncio.run(inject_runner_async(port, content_js_path))

    # There's already a running event loop (e.g. FastAPI with uvicorn async)
    # Run in a separate thread with its own event loop
    result: dict[str, Any] = {}
    error: Exception | None = None

    def _run() -> None:
        nonlocal result, error
        try:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            result = new_loop.run_until_complete(inject_runner_async(port, content_js_path))
        except Exception as exc:
            error = exc
        finally:
            new_loop.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=30)

    if error:
        raise error
    return result
