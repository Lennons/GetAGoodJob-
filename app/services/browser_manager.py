"""
浏览器管理器 — 固定 profile，登录一次持久保持。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Optional

BOSS_JOBS_URL = "https://www.zhipin.com/web/geek/jobs?city=101040100"
CHROME_DATA_DIR = Path.home() / ".boss-chat-assistant-chrome-data"
CDP_PORT = 9223


class BrowserManager:
    """管理 Chrome 生命周期，固定 profile 保持登录。"""

    _instance: Optional["BrowserManager"] = None

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._running = False

    @classmethod
    def instance(cls) -> "BrowserManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def running(self) -> bool:
        return self._running and self._page is not None

    @property
    def page_url(self) -> str:
        try: return self._page.url if (self._page and not self._page.is_closed()) else ""
        except: return ""

    # ── Lifecycle ─────────────────────────────────

    async def start(self) -> None:
        if self._running: return

        CHROME_DATA_DIR.mkdir(parents=True, exist_ok=True)

        # 1. Clean stale lock files
        for lock in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
            lp = CHROME_DATA_DIR / lock
            try: lp.unlink()
            except: pass

        # 2. Kill any existing Chrome on our debug port
        try:
            result = subprocess.run(["lsof","-ti",f":{CDP_PORT}"], capture_output=True, text=True)
            for pid in result.stdout.strip().split("\n"):
                if pid:
                    try: subprocess.run(["kill","-9",pid], capture_output=True)
                    except: pass
        except: pass
        await asyncio.sleep(1)

        # 3. Launch fresh Chrome with independent profile
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        subprocess.Popen([
            chrome,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={CHROME_DATA_DIR}",
            "--no-first-run", "--no-default-browser-check",
            "--window-size=1280,800",
            BOSS_JOBS_URL,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 4. Wait for CDP to be ready
        cdp_url = None
        for _ in range(25):
            await asyncio.sleep(1)
            cdp_url = await self._try_get_cdp()
            if cdp_url: break
        if not cdp_url:
            raise RuntimeError("Chrome 启动超时，请重试")

        # Connect via CDP
        try:
            from patchright.async_api import async_playwright
        except ImportError:
            raise ImportError("patchright 未安装: pip install patchright && patchright install chrome")

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)

        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]
        else:
            self._context = await self._browser.new_context()

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        if "zhipin.com" not in (self._page.url or ""):
            try:
                await self._page.goto(BOSS_JOBS_URL, wait_until="domcontentloaded", timeout=30000)
            except: pass

        self._running = True
        print(f"[BrowserManager] 就绪 — {self._page.url}")

    async def stop(self) -> None:
        """断开 Playwright，Chrome 继续运行（保持登录）。"""
        self._running = False
        try:
            if self._playwright: await self._playwright.stop()
        except: pass
        self._page = None; self._context = None; self._browser = None; self._playwright = None
        BrowserManager._instance = None

    async def close_browser(self) -> None:
        """关闭 Chrome 窗口并清理锁文件，下次启动不会冲突。"""
        self._running = False
        # Close browser via CDP
        try:
            if self._browser: await self._browser.close()
        except: pass
        # Disconnect Playwright
        try:
            if self._playwright: await self._playwright.stop()
        except: pass
        self._page = None; self._context = None; self._browser = None; self._playwright = None
        # Kill any remaining Chrome process on our port
        try:
            import subprocess
            subprocess.run(["lsof","-ti",f":{CDP_PORT}"], capture_output=True)
        except: pass
        await asyncio.sleep(1)
        # Clean lock files so next launch is clean
        for lock in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
            lp = CHROME_DATA_DIR / lock
            try: lp.unlink()
            except: pass
        BrowserManager._instance = None

    # ── Page actions ─────────────────────────────

    async def _ensure_page(self):
        if self._page and not self._page.is_closed(): return
        pages = self._context.pages if self._context else []
        self._page = pages[0] if pages else await self._context.new_page()

    async def navigate(self, url: str) -> dict:
        try:
            await self._ensure_page()
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            return {"ok": True, "url": self._page.url}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def evaluate(self, expression: str) -> Any:
        await self._ensure_page()
        return await self._page.evaluate(expression)

    async def open_tab(self, url: str) -> Any:
        """Open job detail in new tab via window.open — preserves referrer/opener context
        so BOSS serves the same content the user sees on the list page."""
        await self._ensure_page()
        # Use window.open from the list page to get proper referrer + opener context
        url_json = json.dumps(url)
        await self._page.evaluate(f"window.open({url_json}, '_blank')")
        await asyncio.sleep(3)
        # The new tab should now be the latest page in the context
        pages = self._context.pages
        if len(pages) >= 2:
            return pages[-1]
        # Fallback: create page directly
        page = await self._context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000, referer=self._page.url)
        await asyncio.sleep(2)
        return page

    async def evaluate_on(self, page: Any, expression: str) -> Any:
        if not page or page.is_closed():
            raise RuntimeError("详情标签页已关闭")
        return await page.evaluate(expression)

    async def close_tab(self, page: Any) -> None:
        try:
            if page and not page.is_closed():
                await page.close()
        except Exception:
            pass
        try:
            await self._ensure_page()
            await self._page.bring_to_front()
        except Exception:
            pass

    async def press_enter(self) -> None:
        """Press Enter key — native OS event that React can't ignore."""
        await self._ensure_page()
        await self._page.keyboard.press("Enter")

    async def current_url(self) -> str:
        try:
            await self._ensure_page()
            return self._page.url
        except: return ""

    # ── Internal ─────────────────────────────────

    async def _try_get_cdp(self) -> Optional[str]:
        try:
            def _get():
                with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2) as r:
                    return json.loads(r.read()).get("webSocketDebuggerUrl","")
            return await asyncio.to_thread(_get)
        except: return None


# ── Helpers ────────────────────────────────────

def get_browser() -> BrowserManager:
    return BrowserManager.instance()

async def ensure_browser() -> BrowserManager:
    bm = BrowserManager.instance()
    if not bm.running: await bm.start()
    return bm
