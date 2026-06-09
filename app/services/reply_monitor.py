"""Auto-reply monitor — polls BOSS chat list for new messages and replies autonomously."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

from app.services.browser_manager import get_browser
from app.services.deepseek import generate_reply

logger = logging.getLogger("reply_monitor")

BOSS_CHAT_LIST = "https://www.zhipin.com/web/geek/chat"
DEFAULT_POLL_INTERVAL_SEC = 8
REPLY_COOLDOWN_SEC = 5

SCAN_CHAT_LIST_JS = """(() => {
  const items = Array.from(document.querySelectorAll('.chat-conversation-item, .conversation-item, li[class*="conversation"]'));
  const unread = [];
  for (const item of items) {
    const badge = item.querySelector('.unread, .unread-count, [class*="unread"] .badge, [class*="badge"]:not([class*="mute"])');
    const count = badge ? parseInt(badge.textContent?.trim() || '1') : 0;
    if (count <= 0) continue;
    const link = item.querySelector('a');
    const name = item.querySelector('.name, .chat-name, [class*="name"]')?.textContent?.trim() || '';
    const preview = item.querySelector('.msg-preview, .last-msg, [class*="preview"], [class*="last"]')?.textContent?.trim() || '';
    unread.push({ name, preview, url: link ? link.href : null, unreadCount: count });
  }
  return unread;
})()"""

EXTRACT_MESSAGES_JS = """(() => {
  const msgs = Array.from(document.querySelectorAll('.chat-item, .chat-message, .message-item, li[class*="message"]'));
  return msgs.slice(-12).map(m => {
    const isSelf = !!m.querySelector('[class*="self"], [class*="right"], [class*="me"]');
    const textEl = m.querySelector('.text, .msg-text, .content, [class*="text"], [class*="content"]');
    return {
      role: isSelf ? 'user' : 'boss',
      content: textEl ? textEl.textContent.trim() : '',
    };
  }).filter(m => m.content);
})()"""

SEND_REPLY_TMPL = """((text) => {
  const input = document.querySelector('.chat-input, textarea, [contenteditable="true"]');
  if (!input) return false;
  if (input.isContentEditable) {
    input.textContent = text;
  } else {
    input.value = text;
    const ev = new Event('input', {bubbles: true});
    input.dispatchEvent(ev);
  }
  const sendBtn = document.querySelector('.send-btn, button[class*="send"], .btn-send');
  if (sendBtn && sendBtn.offsetParent) {
    sendBtn.click();
  } else {
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, bubbles: true}));
  }
  return true;
})"""

# Extract the job detail link from BOSS chat page header
EXTRACT_JOB_LINK_JS = """(() => {
  // Look for the job detail link in the chat header
  const links = Array.from(document.querySelectorAll('a'));
  for (const a of links) {
    const href = a.href || '';
    if (/job_detail/.test(href)) return href;
  }
  return null;
})()"""

# BOSS 直聘聊天窗口的「发送附件简历」按钮
CLICK_RESUME_BTN_JS = """(() => {
  // 优先找「发送附件简历」或「发简历」按钮
  const btns = Array.from(document.querySelectorAll('button, a, span, div[role="button"]'));
  for (const b of btns) {
    const t = b.textContent || '';
    if (/发送附件简历|发送简历|发简历|附件简历/.test(t) && b.offsetParent) {
      b.click();
      return {ok: true, text: t.trim()};
    }
  }
  // 尝试找简历图标按钮（通常在聊天工具栏）
  const icons = document.querySelectorAll('.icon-resume, [class*="resume"], [class*="attachment-resume"]');
  for (const icon of icons) {
    const btn = icon.closest('button, a, div[role="button"]');
    if (btn && btn.offsetParent) {
      btn.click();
      return {ok: true, text: 'icon'};
    }
  }
  return {ok: false};
})()"""


class ReplyMonitor:

    def __init__(self):
        self._running = False
        self._status = "stopped"
        self._replied_count = 0
        self._replied_urls: set[str] = set()

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
        self._poll_interval = max(3, int(settings.get("reply_poll_seconds", DEFAULT_POLL_INTERVAL_SEC)))
        self._replied_count = 0
        self._replied_urls.clear()

        try:
            bm = get_browser()
            if not bm.running:
                self._status = "no_browser"
                return

            page = await bm.open_tab(BOSS_CHAT_LIST)
            await asyncio.sleep(4)
            self._status = "monitoring"

            while self._running:
                try:
                    # Reload the chat-list page so new unread items appear
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=15000)
                        await asyncio.sleep(2)
                    except Exception:
                        pass

                    unread = await bm.evaluate_on(page, SCAN_CHAT_LIST_JS) or []
                    for chat in unread:
                        if not self._running:
                            break
                        if chat.get("url") and chat["url"] in self._replied_urls:
                            continue
                        await self._reply(bm, page, chat, settings, resume, on_event)
                        self._replied_urls.add(chat["url"])
                        self._replied_count += 1
                        await asyncio.sleep(REPLY_COOLDOWN_SEC)
                except Exception as e:
                    logger.warning(f"Monitor scan: {e}")

                await asyncio.sleep(self._poll_interval)
        except Exception as e:
            logger.error(f"Monitor fatal: {e}")
        finally:
            self._status = "stopped"

    def stop(self):
        self._running = False

    async def _reply(self, bm, monitor_page, chat: dict, settings: dict, resume: dict, on_event):
        chat_page = None
        try:
            chat_page = await bm.open_tab(chat["url"])
            await asyncio.sleep(3)

            messages = await bm.evaluate_on(chat_page, EXTRACT_MESSAGES_JS) or []
            boss_msgs = [m for m in messages if m.get("role") == "boss"]
            if not boss_msgs:
                return

            conv = [{"role": m["role"], "content": m["content"]} for m in messages]

            # Look up job score from the chat page's job detail link
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

            if result.get("action") == "wait":
                return

            text = (result.get("message") or "").strip()

            # BOSS asks for resume → click built-in attachment resume button
            if result.get("action") == "send_resume":
                resume_sent = await bm.evaluate_on(chat_page, CLICK_RESUME_BTN_JS)
                await asyncio.sleep(2)
                if text:
                    safe_text = json.dumps(text, ensure_ascii=False)
                    await bm.evaluate_on(chat_page, f"({SEND_REPLY_TMPL})('{safe_text}')")
                    await asyncio.sleep(2)
                return

            if not text:
                return

            safe_text = json.dumps(text, ensure_ascii=False)
            sent = await bm.evaluate_on(chat_page, f"({SEND_REPLY_TMPL})('{safe_text}')")
            await asyncio.sleep(2)
        except Exception:
            pass
        finally:
            if chat_page:
                try:
                    await bm.close_tab(chat_page)
                except Exception:
                    pass


_monitor: Optional[ReplyMonitor] = None


def get_reply_monitor() -> ReplyMonitor:
    global _monitor
    if _monitor is None:
        _monitor = ReplyMonitor()
    return _monitor
