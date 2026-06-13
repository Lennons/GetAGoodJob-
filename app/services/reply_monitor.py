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
  // Find and click the "发简历" toolbar button (Vue component, class toolbar-btn)
  const el = Array.from(document.querySelectorAll('div.toolbar-btn')).find(e => {
    const t = (e.textContent || '').trim();
    return t === '发简历' || t === '发送简历';
  });
  if (el) {
    el.click();
    return {ok: true, text: (el.textContent || '').trim()};
  }
  // Fallback: broader search
  const all = Array.from(document.querySelectorAll('*'));
  for (const e of all) {
    const t = (e.textContent || '').trim();
    if (t === '发简历' && (e.offsetWidth || e.offsetHeight)) {
      e.click();
      return {ok: true, text: '发简历'};
    }
  }
  return {ok: false, reason: 'resume_btn_not_found'};
})()"""
SELECT_RESUME_IN_DIALOG_JS = """(() => {
  // After clicking "发简历", a dialog "请选择需要投递的简历" appears.
  // We need to: (1) click resume item to select, (2) click enabled "发送" button
  const wait = (ms) => new Promise(r => setTimeout(r, ms));

  return (async () => {
    for (let i = 0; i < 30; i++) {
      await wait(300);

      // Find the active dialog
      const dialog = document.querySelector('.dialog-wrap.active, .boss-dialog__wrapper, [class*="choose-resume"]');
      if (!dialog) continue;

      // Step 1: Click the first resume list item to select it
      const items = dialog.querySelectorAll('.resume-list .list-item, .list-item, li.list-item');
      for (const item of items) {
        if (item.offsetWidth > 0) {
          item.click();
          await wait(500);
          break;
        }
      }

      // Step 2: Find the "发送" (send) button and click it when enabled
      const sendBtn = dialog.querySelector('.btn-confirm, button.btn-sure-v2, button[class*="btn-confirm"]');
      if (sendBtn && !sendBtn.disabled && sendBtn.offsetWidth > 0) {
        sendBtn.click();
        await wait(500);
        return {ok: true, step: 'sent'};
      }
      
      // Also try btn-sure-v2 that's NOT disabled
      const anySendBtn = dialog.querySelector('button.btn-sure-v2:not(.disabled)');
      if (anySendBtn && anySendBtn.offsetWidth > 0) {
        anySendBtn.click();
        await wait(500);
        return {ok: true, step: 'sent_v2'};
      }
    }
    return {ok: false, reason: 'dialog_send_failed'};
  })();
})()"""


EXTRACT_CONTACT_INFO_JS = """(() => {
  // Extract contact name, company and job title from the active chat list item.
  // BOSS chat list HTML structure:
  //   <span class="name-box">
  //     <span class="name-text">姓名</span>
  //     <span>公司名</span>
  //     <i class="vline"></i>
  //     <span>职位</span>
  //   </span>
  const result = { name: "", company: "", title: "" };
  
  // Try active/selected chat item first
  const activeItem = document.querySelector('.friend-content.selected, li[role="listitem"].active, li[role="listitem"][class*="active"]');
  const item = activeItem || document.querySelector('li[role="listitem"]');
  if (!item) return result;
  
  const nameBox = item.querySelector('.name-box');
  if (!nameBox) return result;
  
  // Name: .name-text span
  const nameEl = nameBox.querySelector('.name-text');
  if (nameEl) result.name = (nameEl.textContent || '').trim();
  
  // Company & title: all spans in name-box, skip name-text and skip empty
  const spans = Array.from(nameBox.querySelectorAll('span'));
  let foundNonName = false;
  for (const s of spans) {
    const t = (s.textContent || '').trim();
    if (!t) continue;
    if (s.classList.contains('name-text')) continue;
    if (!foundNonName) {
      result.company = t;
      foundNonName = true;
    } else {
      result.title = t;
      break;
    }
  }
  
  return result;
})()"""

ROLE_SUFFIXES = [
    "猎头顾问", "招聘顾问", "招聘经理", "招聘主管", "招聘专员",
    "人事经理", "人事主管", "人事专员", "招聘助理",
    "HRBP", "HRM", "HRD", "HR", "招聘者",
]

def _extract_role(text: str):
    """Extract recruiter role suffix from concatenated name string.
    e.g., '杨女士思创力维招聘者' -> '招聘者'
    Returns empty string if no known suffix found."""
    for suffix in sorted(ROLE_SUFFIXES, key=len, reverse=True):
        if text.endswith(suffix) and len(text) > len(suffix):
            return suffix
    return ""

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
                    msg = str(e)
                    logger.warning(f"Monitor cycle error: {e}")
                    # Auto-restart browser if connection died
                    if "closed" in msg.lower() or "transport" in msg.lower() or "handler" in msg.lower():
                        try:
                            logger.warning("Browser connection lost, restarting browser...")
                            await bm.stop()
                            await bm.start()
                            chat_page = None
                            await asyncio.sleep(3)
                            logger.info("Browser restarted")
                        except Exception as restart_e:
                            logger.error(f"Browser restart failed: {restart_e}")

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

        # Extract contact name, company and job title from chat panel
        contact_name = ""
        company = chat_name
        job_title = ""
        role = ""
        try:
            info = await bm.evaluate_on(chat_page, EXTRACT_CONTACT_INFO_JS) or {}
            if info.get("name"):
                contact_name = info["name"]
            if info.get("company"):
                company = info["company"]
            if info.get("title"):
                job_title = info["title"]
        except Exception:
            pass
        # Extract recruiter role from chat_name suffix
        # (chat panel .name-box only has name/company/job-position,
        #  the recruiter's role like '招聘者/HRBP' is only in the list item text)
        role = _extract_role(chat_name)

        # Read messages
        messages = await bm.evaluate_on(chat_page, READ_MESSAGES_JS) or []
        logger.info(f"Read {len(messages)} messages from {chat_name}")

        boss_msgs = [m for m in messages if m.get("role") == "boss"]
        if not boss_msgs:
            logger.info(f"No boss messages for {chat_name}")
            await self._return_to_list(bm, chat_page)
            return

        conv = [{"role": m["role"], "content": m["content"]} for m in messages]

        # Look up job data (score + full description)
        job_data = None
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
                if isinstance(job_data, dict):
                    job_score = job_data.get("score", 0)
        except Exception:
            pass

        result = await generate_reply(resume, job_data, conv, settings, job_score=job_score)

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
            # Step 1: Click "发简历" toolbar button using native Playwright click
            # (JS click() doesn't trigger Vue event handlers on BOSS)
            pos_json = await bm.evaluate_on(chat_page, """(() => {
                const el = Array.from(document.querySelectorAll('div.toolbar-btn')).find(e => {
                    const t = (e.textContent || '').trim();
                    return t === '发简历' || t === '发送简历';
                });
                if (!el) return JSON.stringify(null);
                const r = el.getBoundingClientRect();
                return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
            })()""")
            import json as _json
            pos = _json.loads(pos_json)
            if pos:
                await chat_page.mouse.click(pos['x'], pos['y'])
                logger.info(f"Native click on 发简历 at ({pos['x']}, {pos['y']})")
            else:
                logger.warning(f"Resume button not found for {chat_name}")
            await asyncio.sleep(2)
            # Step 2: Handle the resume dialog — select resume and click send
            dialog_result = await bm.evaluate_on(chat_page, SELECT_RESUME_IN_DIALOG_JS)
            logger.info(f"Resume dialog result: {dialog_result}")
            await asyncio.sleep(2)

        if text:
            # Focus and clear the chat input
            focused = await bm.evaluate_on(chat_page, """(() => {
              var input = document.querySelector('#chat-input') || document.querySelector('[contenteditable="true"]');
              if (!input) return false;
              input.focus();
              input.click();
              if (input.isContentEditable) { input.textContent = ''; }
              else { input.value = ''; input.dispatchEvent(new Event('input', {bubbles:true})); }
              return true;
            })()""")
            if not focused:
                logger.warning("Chat input not found")
                await self._return_to_list(bm, chat_page)
                return
            await asyncio.sleep(0.3)
            # Type character by character with realistic delays (same as human typing)
            await chat_page.keyboard.type(text, delay=60)
            await asyncio.sleep(0.4)
            await chat_page.keyboard.press("Enter")
            await asyncio.sleep(2)
            logger.info(f"Reply sent to {chat_name}: {text[:60]}...")

            # Log the reply
            try:
                log_data = json.dumps({
                    "contact_name": contact_name,
                    "company": company,
                    "title": job_title,
                    "role": role,
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
