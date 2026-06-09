"""Auto-reply monitor — polls BOSS chat list for new messages and replies autonomously.
Uses Vue component data for reading and Playwright mouse click for SPA navigation."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from app.services.browser_manager import get_browser
from app.services.deepseek import generate_reply

logger = logging.getLogger("reply_monitor")
_log_handler = None


def _ensure_log_handler():
    global _log_handler
    if _log_handler:
        return
    from pathlib import Path
    log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "reply_monitor.log"
    _log_handler = logging.FileHandler(str(log_file), encoding="utf-8")
    _log_handler.setLevel(logging.DEBUG)
    _log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_log_handler)
    logger.setLevel(logging.DEBUG)
    stream = logging.StreamHandler()
    stream.setLevel(logging.INFO)
    stream.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    logger.addHandler(stream)


BOSS_CHAT_LIST = "https://www.zhipin.com/web/geek/chat"
DEFAULT_POLL_INTERVAL_SEC = 15
REPLY_COOLDOWN_SEC = 8

# ── JS snippets ────────────────────────────────────

SCAN_UNREAD_VIA_VUE = r"""(() => {
  // Scan unread conversations using DOM selectors
  var lis = document.querySelectorAll('li[role="listitem"]');
  var unread = [];
  for (var i = 0; i < lis.length; i++) {
    var li = lis[i];
    var badge = li.querySelector('.notice-badge');
    if (!badge) continue;
    var count = parseInt(badge.textContent.trim());
    if (!count || count <= 0) continue;
    var nameEl = li.querySelector('.name-text');
    unread.push({
      idx: i,
      name: nameEl ? nameEl.textContent.trim() : '',
      unreadCount: count,
      lastText: li.querySelector('.last-msg-text') ? li.querySelector('.last-msg-text').textContent.trim() : '',
      encryptJobId: '',
    });
  }
  return unread;
})()"""

READ_MESSAGES_JS = r"""(() => {
  // Read visible messages from the right-side chat panel
  const result = [];
  const seen = new Set();
  const vw = window.innerWidth || 1200;

  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = text => (text || '')
    .replace(/^(已读|未读|送达|发送失败|已发送)\s*/g, '')
    .replace(/\n?(已读|未读|送达|发送失败|已发送)$/g, '')
    .trim();

  // Prefer li.message-item or [class^="message-item_"]
  let items = document.querySelectorAll('li.message-item, li[class*="message-item"], [class^="message-item_"], [class*="chat-message"]');

  for (const el of items) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 80 || r.height < 12 || r.height > 400) continue;
    if (r.left + r.width / 2 < vw * 0.35) continue;
    if (['BUTTON', 'INPUT', 'A', 'NAV', 'HEADER', 'FOOTER'].includes(el.tagName)) continue;

    const fullText = el.innerText || '';
    const content = clean(fullText);
    if (!content || content.length > 1000) continue;

    const cls = el.className || '';
    const isSelf = cls.includes('item-myself') || cls.includes('myself') || cls.includes('self') || r.left > vw * 0.52;

    const dedupKey = (isSelf ? 'me' : 'boss') + '|' + content.substring(0, 80);
    if (seen.has(dedupKey)) continue;
    seen.add(dedupKey);

    result.push({ role: isSelf ? 'user' : 'boss', content });
  }
  return result;
})()"""

CLICK_UNREAD_TAB_JS = """(() => {
  const spans = document.querySelectorAll('span.label-name');
  for (const s of spans) {
    if ((s.textContent || '').trim().startsWith('未读')) {
      s.click();
      return true;
    }
  }
  return false;
})()"""

EXTRACT_JOB_LINK_JS = """(() => {
  const links = Array.from(document.querySelectorAll('a'));
  for (const a of links) {
    const href = a.href || '';
    if (/job_detail/.test(href)) return href;
  }
  return null;
})()"""

CLICK_RESUME_BTN_JS = """(() => {
  const btns = Array.from(document.querySelectorAll('button, a, span, div[role="button"]'));
  for (const b of btns) {
    const t = b.textContent || '';
    if (/发送附件简历|发送简历|发简历|附件简历/.test(t) && b.offsetParent) {
      b.click();
      return {ok: true, text: t.trim()};
    }
  }
  return {ok: false};
})()"""



EXTRACT_COMPANY_TITLE_JS = """(() => {
  const result = { company: "", title: "" };
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const els = Array.from(document.querySelectorAll('.name-text, .company-name, .job-name, .chat-top-name, [class*="name"], [class*="title"]'));
  for (const el of els) {
    if (!visible(el)) continue;
    const t = (el.textContent || "").trim();
    if (!t || t.length > 50) continue;
    if (!result.company && (t.includes("公司") || t.includes("科技") || t.includes("集团") || t.length > 4)) result.company = t;
    if (!result.title && (t.includes("经理") || t.includes("工程师") || t.includes("设计师") || t.includes("产品"))) result.title = t;
  }
  const linkEl = Array.from(document.querySelectorAll('a')).find(a => /job_detail/.test(a.href || ""));
  if (linkEl) {
    const linkText = (linkEl.textContent || "").trim();
    if (linkText && linkText.length < 50 && linkText.length > 2) result.title = result.title || linkText;
  }
  return result;
})()"""
class ReplyMonitor:

    def __init__(self):
        _ensure_log_handler()
        self._running = False
        self._status = "stopped"
        self._replied_count = 0
        self._replied_ids: set[str] = set()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def status(self) -> str:
        return self._status

    @property
    def replied_count(self) -> int:
        return self._replied_count

    async def start(self, settings: dict, resume: dict, on_event=None):
        if self._running:
            return
        self._running = True
        self._status = "starting"
        poll_sec = max(5, int(settings.get("reply_poll_seconds", DEFAULT_POLL_INTERVAL_SEC)))
        self._replied_count = 0
        self._replied_ids.clear()

        try:
            bm = get_browser()
            if not bm.running:
                self._status = "no_browser"
                return

            self._status = "monitoring"
            chat_page = None

            while self._running:
                try:
                    logger.info("--- New poll cycle ---")
                    chat_page = await self._ensure_chat_page(bm, chat_page)
                    if not chat_page:
                        await asyncio.sleep(poll_sec)
                        continue

                    # Ensure we're viewing the chat list (click "未读" tab)
                    try:
                        await bm.evaluate_on(chat_page, CLICK_UNREAD_TAB_JS)
                        await asyncio.sleep(1.5)
                    except Exception:
                        pass

                    # Scan unread via Vue data
                    unread = await bm.evaluate_on(chat_page, SCAN_UNREAD_VIA_VUE) or []
                    logger.info(f"Scan: {len(unread)} unread conversations")
                    for u in unread[:5]:
                        logger.info(f"  [{u.get('idx')}] {u.get('name')} x{u.get('unreadCount')}: {u.get('lastText', '')[:50]}")

                    if not unread:
                        await asyncio.sleep(poll_sec)
                        continue

                    # Handle first unread each cycle
                    chat = unread[0]
                    chat_id = chat.get("encryptJobId", "")
                    if chat_id and chat_id in self._replied_ids:
                        logger.info(f"Already replied to job {chat_id}, waiting for new messages")
                        await asyncio.sleep(poll_sec)
                        continue

                    await self._handle_one(bm, chat_page, chat, settings, resume)
                    if chat_id:
                        self._replied_ids.add(chat_id)
                    self._replied_count += 1

                except Exception as e:
                    logger.warning(f"Monitor cycle error: {e}")

                # Sleep in 1s chunks so stop is responsive
                for _ in range(poll_sec):
                    if not self._running:
                        break
                    await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Monitor fatal: {e}")
        finally:
            self._status = "stopped"

    def stop(self):
        self._running = False

    async def _ensure_chat_page(self, bm, chat_page):
        """Return a valid Playwright page showing the BOSS chat list."""
        try:
            if chat_page and not chat_page.is_closed():
                url = await bm.evaluate_on(chat_page, "location.href") or ""
                if "/web/geek/chat" in url:
                    return chat_page
        except Exception:
            pass
        # Open fresh chat page
        try:
            if not self._running:
                return None
            logger.info("Opening new chat page...")
            chat_page = await bm.open_tab(BOSS_CHAT_LIST)
            await asyncio.sleep(3)
            return chat_page
        except Exception:
            logger.error("Failed to open chat page")
            return None

    async def _handle_one(self, bm, chat_page, chat: dict, settings: dict, resume: dict):
        """Click unread LI, read messages, reply, then return to list."""
        if not self._running:
            return
        chat_idx = chat.get("idx", 0)
        chat_name = chat.get("name", "?")
        logger.info(f"Opening: [{chat_idx}] {chat_name}")

        # Click the LI using Playwright real mouse click
        selector = f'li[role="listitem"]:nth-child({chat_idx + 1})'
        clicked = await bm.click_on(chat_page, selector)
        if not clicked:
            logger.warning(f"Failed to click LI idx={chat_idx}")
            return

        # Wait for chat panel to render
        await asyncio.sleep(3)

        # Verify we're still on chat page
        try:
            url = await bm.evaluate_on(chat_page, "location.href") or ""
            if "/web/geek/chat" not in url:
                logger.warning(f"Navigated away: {url[:80]}")
                return
        except Exception:
            return

        # Extract company and job title from chat panel
        company = chat_name
        job_title = ""
        try:
            info = await bm.evaluate_on(chat_page, EXTRACT_COMPANY_TITLE_JS) or {}
            if info.get("company"):
                company = info["company"]
            if info.get("title"):
                job_title = info["title"]
        except Exception:
            pass

        # Read messages
        messages = await bm.evaluate_on(chat_page, READ_MESSAGES_JS) or []
        logger.info(f"Read {len(messages)} messages from {chat_name}")

        boss_msgs = [m for m in messages if m.get("role") == "boss"]
        if not boss_msgs:
            logger.info(f"No boss messages for {chat_name}")
            await self._return_to_list(bm, chat_page)
            return

        conv = [{"role": m["role"], "content": m["content"]} for m in messages]

        # Look up job score
        job_score = 0
        try:
            job_url = await bm.evaluate_on(chat_page, EXTRACT_JOB_LINK_JS)
            if job_url:
                import urllib.request as _req
                data = json.dumps({"source_key": job_url}).encode()
                resp = await asyncio.to_thread(lambda: _req.urlopen(_req.Request(
                    "http://127.0.0.1:8788/api/jobs/lookup",
                    data=data, headers={"Content-Type": "application/json"}, method="POST"
                ), timeout=5).read())
                job_data = json.loads(resp)
                job_score = job_data.get("score", 0) if isinstance(job_data, dict) else 0
        except Exception:
            pass

        result = await generate_reply(resume, None, conv, settings, job_score=job_score)

        action = result.get("action", "")
        text = (result.get("message") or "").strip()

        if action == "wait":
            logger.info(f"AI: wait for {chat_name}")
            await self._return_to_list(bm, chat_page)
            return

        # Natural delay — check stop signal every second
        import random as _random
        delay = _random.randint(30, 60)
        logger.info(f"Waiting {delay}s before replying (natural pacing)...")
        for _ in range(delay):
            if not self._running:
                logger.info("Reply cancelled (monitor stopped)")
                await self._return_to_list(bm, chat_page)
                return
            await asyncio.sleep(1)

        if action == "send_resume":
            await bm.evaluate_on(chat_page, CLICK_RESUME_BTN_JS)
            await asyncio.sleep(1.5)

        if text:
            # Type reply into chat input and send
            safe_text = json.dumps(text, ensure_ascii=False)
            await bm.evaluate_on(chat_page, f"""((text) => {{
              var input = document.querySelector('#chat-input') || document.querySelector('[contenteditable="true"]');
              if (!input) return false;
              input.focus();
              input.click();
              if (input.isContentEditable) {{ input.textContent = ''; }} else {{ input.value = ''; }}
              for (var i=0;i<text.length;i++) {{
                if (input.isContentEditable) input.textContent += text[i];
                else input.value += text[i];
                input.dispatchEvent(new Event('input', {{bubbles:true}}));
              }}
              return true;
            }})({safe_text})""")
            await asyncio.sleep(0.5)
            await bm.evaluate_on(chat_page, """(() => {
              var input = document.querySelector('#chat-input') || document.querySelector('[contenteditable="true"]');
              if (!input) return false;
              input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', keyCode:13, bubbles:true, composed:true}));
              return true;
            })()""")
            await asyncio.sleep(2)
            logger.info(f"Reply sent to {chat_name}: {text[:60]}...")

            # Log the reply
            try:
                log_data = json.dumps({
                    "company": company,
                    "title": job_title,
                    "message": text[:500],
                }).encode()
                await asyncio.to_thread(lambda: __import__("urllib.request").request.urlopen(
                    __import__("urllib.request").request.Request(
                        "http://127.0.0.1:8788/api/reply-logs",
                        data=log_data,
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    ), timeout=5
                ).read())
            except Exception as log_err:
                logger.warning(f"Failed to log reply: {log_err}")

        # Return to chat list
        await self._return_to_list(bm, chat_page)

    async def _return_to_list(self, bm, chat_page):
        """Return to the unread chat list view."""
        try:
            await bm.evaluate_on(chat_page, CLICK_UNREAD_TAB_JS)
            await asyncio.sleep(1)
        except Exception:
            pass


_monitor: Optional[ReplyMonitor] = None


def get_reply_monitor() -> ReplyMonitor:
    global _monitor
    if _monitor is None:
        _monitor = ReplyMonitor()
    return _monitor
