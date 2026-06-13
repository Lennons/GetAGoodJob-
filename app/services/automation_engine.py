"""
BOSS直聘 自动化引擎 — Async Patchright，全异步无 greenlet 冲突。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time as time_module
from typing import Any, Optional

from app.services.browser_manager import get_browser

RISK_PATTERNS = re.compile(
    r"(验证码|账号异常|请完成验证|滑块验证|行为验证|请稍后再试|"
    r"今日沟通次数已达上限|操作太频繁|"
    r"请先登录|当前登录状态已失效|登录失效|登录状态失效|重新登录|登录过期|登录超时|"
    r"请重新登录|身份过期|身份认证失败)",
    re.IGNORECASE,
)

LOGIN_URL_PATTERNS = re.compile(
    r"(/web/geek/login|/account/login|/login\b)",
    re.IGNORECASE,
)

LOGIN_PAGE_DETECT_JS = """(() => {
  const inputs = document.querySelectorAll('input[placeholder*="手机"], input[placeholder*="验证码"]');
  const text = (document.body.innerText || '').slice(0, 200);
  const hasLogin = inputs.length > 0 && (text.includes('登录') || text.includes('扫码') || text.includes('验证码登录'));
  return {url: location.href, hasLoginForm: hasLogin, inputs: inputs.length};
})()"""


HARD_DAILY_LIMIT = 80
LONG_BREAK_EVERY_N = 8
LONG_BREAK_MIN_SEC = 45
LONG_BREAK_MAX_SEC = 120
TARGET_JOBS_URL = "https://www.zhipin.com/web/geek/jobs?city=101040100"
TARGET_JOB_KEYWORD = "产品经理"
DEFAULT_CITY = "重庆"
MAX_EMPTY_SCROLL_ROUNDS = 10

# ── JS snippets ────────────────────────────────────

def _select_recommended_job_tab_js(label: str) -> str:
    target = json.dumps(label, ensure_ascii=False)
    return f"""
(() => {{
  const target = {target};
  const normalize = (text) => String(text || '').replace(/（/g, '(').replace(/）/g, ')').replace(/\\s+/g, '').trim();
  const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const nodes = Array.from(document.querySelectorAll('.c-expect-select a, .expect-list a, a.expect-item'));
  const matched = nodes.find((el) => {{
    if (!visible(el)) return false;
    const text = normalize(el.textContent);
    return text === normalize(target);
  }});
  if (!matched) return {{ ok: false, reason: 'not_found', target }};
  const clickable = matched.closest('a, button, [role="button"]') || matched;
  clickable.scrollIntoView?.({{ block: 'center', inline: 'center' }});
  clickable.click();
  return {{ ok: true, target, text: matched.textContent.trim(), url: location.href }};
}})()
"""

def _open_city_dialog_js() -> str:
    return """
(() => {
  const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const openDialog = Array.from(document.querySelectorAll('.city-select-dialog, .dialog-wrap.city-select-dialog')).find(visible);
  if (openDialog) {
    return { ok: true, alreadyOpen: true };
  }
  const el = Array.from(document.querySelectorAll('.city-label, .cur-city-label')).find(visible);
  if (!el) return { ok: false, reason: 'city_filter_not_found' };
  const clickable = el.closest('.city-label, button, a, [role="button"]') || el;
  clickable.scrollIntoView?.({ block: 'center', inline: 'center' });
  clickable.click();
  return { ok: true, text: clickable.textContent.trim() };
})()
"""

def _select_city_option_js(city: str) -> str:
    city_json = json.dumps(city, ensure_ascii=False)
    return f"""
(() => {{
  const city = {city_json};
  const normalize = (text) => String(text || '').replace(/\\s+/g, '').trim();
  const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const nodes = Array.from(document.querySelectorAll('.city-select-dialog li, .dialog-wrap.city-select-dialog li, .dialog-wrap li'));
  const matched = nodes.find((el) => visible(el) && normalize(el.textContent) === normalize(city));
  if (!matched) return {{ ok: false, reason: 'city_not_found', city }};
  matched.scrollIntoView?.({{ block: 'center', inline: 'center' }});
  matched.click();
  return {{ ok: true, city, text: matched.textContent.trim() }};
}})()
"""

def _norm_title(value: Any) -> str:
    return re.sub(r"[\s（）()【】\\[\\]·,，/\\-—_]+", "", str(value or "")).lower()

def _same_job_title(left: Any, right: Any) -> bool:
    a = _norm_title(left)
    b = _norm_title(right)
    if not a or not b:
        return True
    return a in b or b in a


def _select_job_card_js(source_key: str, url: str) -> str:
    source_json = json.dumps(source_key or "", ensure_ascii=False)
    url_json = json.dumps(url or "", ensure_ascii=False)
    return f"""
(() => {{
  const sourceKey = {source_json};
  const targetUrl = {url_json};
  const cleanUrl = (value) => String(value || '').split('?')[0];
  const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const links = Array.from(document.querySelectorAll('a.job-name[href*="/job_detail/"], a[href*="/job_detail/"]')).filter(visible);
  const matched = links.find((a) => cleanUrl(a.href) === cleanUrl(sourceKey) || cleanUrl(a.href) === cleanUrl(targetUrl));
  if (!matched) return {{ ok: false, reason: 'job_card_not_found', sourceKey, targetUrl }};
  matched.scrollIntoView?.({{ block: 'center', inline: 'center' }});
  matched.click();
  return {{ ok: true, title: matched.textContent.trim(), href: matched.href }};
}})()
"""


def _skip_evaluation(reason: str, *, score: int = 0, risks: Optional[list] = None) -> dict:
    return {
        "score": score,
        "decision": "skip",
        "status": "skipped",
        "reasons": [reason],
        "risks": risks or [],
        "best_resume_angle": "",
        "initial_message": "",
    }


def _looks_like_initial_message(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(re.search(r"^(您好|你好|Hi|Hello)|我对.+感兴趣|期待沟通|方便的话", text, re.IGNORECASE))


def _mark_evaluation_skipped(evaluation: Optional[dict], reason: str) -> dict:
    result = dict(evaluation or {})
    # AI reasons first, technical/skip-line reason last
    reasons = []
    for item in result.get("reasons") or []:
        if item and item not in reasons and not _looks_like_initial_message(item):
            reasons.append(item)
    if reason and reason not in reasons:
        reasons.append(reason)
    result["decision"] = "skip"
    result["status"] = "skipped"
    result["reasons"] = reasons
    result["initial_message"] = ""
    return result


EXTRACT_JOB_LIST_JS = """
(() => {
  const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const clean = (text) => String(text || '').replace(/\\s+/g, ' ').trim();
  const jobs = [];
  const seen = new Set();
  const cards = Array.from(document.querySelectorAll('.job-card-box')).filter(visible);
  for (const card of cards) {
    const a = card.querySelector('a.job-name[href*="job_detail"], a[href*="/job_detail/"]');
    if (!a) continue;
    const href = a.href;
    if (!href || seen.has(href) || href.includes('#') || !href.includes('/job_detail/')) continue;
    seen.add(href);
    const title = clean(a.textContent);
    const salary = clean(card.querySelector('.job-salary, [class*="salary"]')?.textContent);
    const company = clean(card.querySelector('.boss-name, .company-name, [class*="company-name"], [class*="brand"]')?.textContent);
    const city = clean(card.querySelector('.company-location, [class*="location"], [class*="area"]')?.textContent);
    const tags = Array.from(card.querySelectorAll('.tag-list li')).map((li) => clean(li.textContent)).filter(Boolean);
    const text = clean(card.innerText);
    jobs.push({
      source_key: href.split('?')[0],
      url: href,
      title,
      salary,
      company,
      city,
      description: text,
      raw: { card_text: text, tags }
    });
  }
  return jobs;
})()
"""

SCROLL_JOB_LIST_JS = """
(() => {
  // 找到 BOSS 直聘岗位列表的实际滚动容器并滚到底部
  const findScrollable = () => {
    // 优先 BOSS 的岗位列表容器
    for (const sel of ['.job-list-box', '.job-list-container', '.recommend-result-job', '.recommend-result-inner']) {
      const el = document.querySelector(sel);
      if (el && el.scrollHeight > el.clientHeight + 10) return el;
    }
    // 回退到页面滚动
    if (document.scrollingElement && document.scrollingElement.scrollHeight > document.scrollingElement.clientHeight + 10)
      return document.scrollingElement;
    return document.documentElement;
  };
  const container = findScrollable();
  const prevCards = document.querySelectorAll('.job-card-box').length;
  // 直接滚到底部（不是按高度增量，而是 scrollTop = scrollHeight）
  container.scrollTop = container.scrollHeight;
  // 再微调以触发懒加载
  setTimeout(() => { container.scrollTop = container.scrollHeight; }, 100);
  return { cardCount: prevCards, scrollTop: container.scrollTop, scrollHeight: container.scrollHeight };
})()
"""

SEARCH_AND_SUBMIT_JS = """
(() => {
  const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const keyword = "KEYWORD_PLACEHOLDER";
  let input = document.querySelector('.c-search-input input, .job-search-form input, .expect-search-inner input[type="text"]');
  if (!input || !visible(input)) {
    input = Array.from(document.querySelectorAll('input[type="text"], input:not([type])')).find(
      (el) => visible(el) && /搜索|职位|公司|岗位/.test(el.placeholder || '')
    );
  }
  if (!input || !visible(input)) return { ok: false, reason: 'search_input_not_found', keyword };
  input.focus();
  input.value = '';
  input.dispatchEvent(new Event('focus', { bubbles: true }));
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  if (setter && setter.set) { setter.set.call(input, keyword); }
  else { input.value = keyword; }
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Unidentified', bubbles: true }));
  input.focus();
  return { ok: true, keyword, filled: true };
})()
"""

def _target_list_state_js(label: str, keyword: str) -> str:
    label_json = json.dumps(label, ensure_ascii=False)
    keyword_json = json.dumps(keyword, ensure_ascii=False)
    city_json = json.dumps(city, ensure_ascii=False)
    return f"""
(() => {{
  const label = {label_json};
  const keyword = {keyword_json};
  const city = {city_json};
  const findVmByName = (name) => {{
    const root = document.querySelector('#wrap')?.__vue__;
    const stack = root ? [root] : [];
    while (stack.length) {{
      const vm = stack.shift();
      if (vm?.$options?.name === name) return vm;
      stack.push(...(vm?.$children || []));
    }}
    return null;
  }};
  const normalize = (text) => String(text || '').replace(/（/g, '(').replace(/）/g, ')').replace(/\\s+/g, '').trim();
  const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const expectVm = findVmByName('vue-component-80-ExpectSelect') || document.querySelector('.c-expect-select')?.__vue__;
  const pageVm = findVmByName('PageJobs');
  const expectedId = expectVm?.encryptExpectId || expectVm?.expectList?.[0]?.encryptId || '';
  const pageExpectId = pageVm?.formData?.encryptExpectId || pageVm?.catchExpectId || '';
  const currentJobTab = expectVm?.currentJobTab || '';
  const recommend = document.querySelector('.c-expect-select a.synthesis, a.synthesis');
  const recommendActive = !!(recommend && /active|selected|current|cur/.test(String(recommend.className || '')));
  const expectNodes = Array.from(document.querySelectorAll('.c-expect-select a, .expect-list a, a.expect-item')).filter(visible);
  const expectActive = expectNodes.some((el) => normalize(el.textContent) === normalize(label) && /active|selected|current|cur/.test(String(el.className || '')));
  const cityLabel = normalize(document.querySelector('.cur-city-label')?.textContent || document.querySelector('.city-label')?.textContent || '');
  const citySelected = cityLabel === normalize(city);
  const topText = Array.from(document.querySelectorAll('.expect-and-search, .c-expect-select, .expect-list'))
    .filter(visible)
    .map((el) => normalize(el.innerText || el.textContent))
    .join('|');
  const cards = Array.from(document.querySelectorAll('.job-card-box')).filter(visible);
  const sampleTitles = cards.slice(0, 8).map((card) => normalize(card.querySelector('a.job-name')?.textContent || card.innerText));
  const sampleCards = cards.slice(0, 8).map((card) => normalize(card.innerText));
  const productLikeCount = sampleTitles.filter((title) => title.includes(keyword.replace(/\\s+/g, ''))).length;
  const cityLikeCount = sampleCards.filter((text) => text.includes('重庆')).length;
  const strictExpectActive = currentJobTab === 'expect' && !!expectedId && pageExpectId === expectedId && expectActive && citySelected;
  const visibleExpectActive = expectActive && citySelected && topText.includes(normalize(label)) && productLikeCount > 0;
  const targetListVisible = topText.includes(normalize(label)) && productLikeCount > 0;
  return {{
    ok: cards.length > 0 && citySelected && (strictExpectActive || (visibleExpectActive && targetListVisible)),
    url: location.href,
    topText,
    cityLabel,
    citySelected,
    cardCount: cards.length,
    productLikeCount,
    cityLikeCount,
    sampleTitles,
    currentJobTab,
    expectedId,
    pageExpectId,
    expectActive,
    recommendActive,
    strictExpectActive,
    visibleExpectActive,
    targetListVisible,
  }};
}})()
"""

EXTRACT_JOB_DETAIL_JS = """
(() => {
  const get = (sel) => { const el = document.querySelector(sel); return el ? el.textContent.trim() : ''; };

  let title = get('.job-name') || get('[class*="job-name"]') || get('.name');
  if (!title || title.length > 80) title = get('h1') || (document.title||'').split('招聘')[0] || '';
  title = title.replace(/\\n\\s+/g, ' ').trim().slice(0, 120);

  let company = get('.company-name') || get('[class*="company-name"]');
  if (!company || company === title) {
    const links = document.querySelectorAll('a[href*="company"]');
    for (const a of links) { const t = a.textContent.trim(); if (t && t.length < 60 && t !== title) { company = t; break; } }
  }
  company = company.replace(/\\n\\s+/g, ' ').trim().slice(0, 120);

  let salary = get('.salary') || get('[class*="salary"]');
  salary = salary.replace(/\\n\\s+/g, ' ').trim().slice(0, 80);

  let city = get('[class*="location"]') || get('[class*="area"]') || '';
  city = city.replace(/\\n\\s+/g, ' ').trim().slice(0, 40);

  // Description: try EVERY possible BOSS container
  let desc = '';
  const selList = [
    '.job-sec-text', '.job-detail', '.detail-content', '.job_detail',
    '[class*="job-sec"]', '[class*="job-detail"]', '[class*="detail-content"]',
    '[class*="job_detail"]', '[class*="description"]', '[class*="job-description"]',
    '.job-desc', '[class*="job-desc"]', '.job-content', '[class*="job-content"]',
    '.job-requirement', '[class*="requirement"]',
  ];
  for (const sel of selList) {
    try {
      const el = document.querySelector(sel);
      if (el && el.innerText && el.innerText.length > 50) { desc = el.innerText; break; }
    } catch(e) {}
  }
  // If still no desc, try finding the largest text block on the page
  if (!desc || desc.length < 50) {
    const blocks = Array.from(document.querySelectorAll('div, section, article'))
      .filter(d => d.innerText && d.innerText.length > 100 && d.innerText.length < 10000)
      .sort((a,b) => b.innerText.length - a.innerText.length);
    if (blocks.length > 0) desc = blocks[0].innerText;
  }
  if (!desc || desc.length < 50) desc = document.body?.innerText || '';
  desc = desc.slice(0, 8000);

  return {
    source_key: location.href.split('?')[0], url: location.href,
    title, company, salary, city,
    description: desc,
    raw: { pageTitle: document.title }
  };
})()
"""

EXTRACT_SELECTED_JOB_DETAIL_JS = """
(() => {
  const clean = (text) => String(text || '').replace(/\\s+/g, ' ').trim();
  const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const panel = Array.from(document.querySelectorAll('.job-detail-container, .job-detail-box, [class*="job-detail"]'))
    .find((el) => visible(el) && clean(el.innerText).length > 80) || document.body;
  const get = (sel) => clean(panel.querySelector(sel)?.textContent || '');
  let title = get('.job-detail-info .job-name') || get('.job-name') || get('h1');
  let salary = get('.job-detail-info .salary') || get('.salary') || get('[class*="salary"]');
  let city = '';
  const detailInfo = panel.querySelector('.job-detail-info');
  if (detailInfo) {
    const lis = Array.from(detailInfo.querySelectorAll('li')).map((li) => clean(li.textContent)).filter(Boolean);
    city = lis.find((x) => /北京|上海|广州|深圳|重庆|成都|杭州|苏州|武汉|西安|南京|天津/.test(x)) || '';
  }
  if (!city) city = get('[class*="location"]') || get('[class*="area"]');
  let company = get('.boss-name') || get('.company-name') || get('[class*="company-name"]');
  let desc = '';
  const descNode = Array.from(panel.querySelectorAll('.job-sec-text, [class*="job-sec"], [class*="job-detail"], [class*="description"], [class*="job-desc"]'))
    .filter((el) => visible(el) && clean(el.innerText).length > 50)
    .sort((a, b) => clean(b.innerText).length - clean(a.innerText).length)[0];
  if (descNode) desc = clean(descNode.innerText);
  if (!desc) desc = clean(panel.innerText);
  return {
    source_key: "",
    url: location.href,
    title: title.slice(0, 120),
    company: company.slice(0, 120),
    salary: salary.slice(0, 80),
    city: city.slice(0, 80),
    description: desc.slice(0, 8000),
    raw: { pageTitle: document.title, from_list_panel: true }
  };
})()
"""

FIND_AND_CLICK_CHAT_BTN_JS = """
(() => {
  const patterns = [/^立即沟通$/, /^立即溝通$/, /^沟通$/, /^开聊$/, /^立即开聊$/, /^感兴趣$/];
  const all = Array.from(document.querySelectorAll('button, a, span[role="button"], div[role="button"]'));
  for (const pat of patterns) {
    const btn = all.find(b => pat.test((b.textContent||'').replace(/\\s/g,'')) && b.offsetParent !== null);
    if (btn) { btn.click(); return {found: true, text: btn.textContent.trim()}; }
  }
  const fb = all.find(b => /沟通|开聊/.test(b.textContent||'') && b.offsetParent !== null);
  if (fb) { fb.click(); return {found: true, text: fb.textContent.trim(), fallback: true}; }
  return {found: false};
})()
"""

WAIT_FOR_CHAT_INPUT_JS = """
(() => {
  const cs = document.querySelectorAll('[class*="dialog"], [class*="chat"], [class*="modal"], [class*="drawer"], body');
  for (const c of cs) {
    if (!c.offsetParent) continue;
    const inp = c.querySelector('[contenteditable="true"], [role="textbox"], textarea');
    if (inp && inp.offsetParent) return true;
  }
  return false;
})()
"""

def _fill_and_send_js(msg: str) -> str:
    """Fill message and click send in BOSS chat. Tries: text button, icon button, Enter, Ctrl+Enter."""
    m = json.dumps(msg, ensure_ascii=False)
    return """( () => {
  const msg = """ + m + """;

  // Find input
  const cs = document.querySelectorAll('[class*="dialog"], [class*="chat"], [class*="modal"], [class*="drawer"], [class*="panel"], body');
  let input = null;
  let container = null;
  for (const c of cs) { if (!c.offsetParent) continue; input = c.querySelector('textarea, [contenteditable="true"], [role="textbox"]'); if (input && input.offsetParent) { container = c; break; } }
  if (!input) return {ok:false,error:'no_input'};

  // Clear and fill
  input.focus();
  if (input.isContentEditable || input.getAttribute('contenteditable')==='true') {
    input.textContent = '';
    input.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'deleteContent'}));
    input.textContent = msg;
    input.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:msg}));
  } else {
    input.value = '';
    input.dispatchEvent(new Event('input',{bubbles:true}));
    const s = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value') || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
    if(s?.set) s.set.call(input, msg); else input.value = msg;
    input.dispatchEvent(new Event('input',{bubbles:true}));
    input.dispatchEvent(new Event('change',{bubbles:true}));
  }

  // Try to find and click send button — multiple strategies
  const allBtns = Array.from(document.querySelectorAll('button, span[role="button"], div[role="button"], a.btn'));
  const visible = (b) => b.offsetParent !== null || b.getClientRects().length > 0;

  // Strategy 1: exact text match
  for (const pat of [/^发送$/, /^Send$/i, /^打招呼$/, /^发送招呼$/, /^确认发送$/]) {
    const btn = allBtns.find(b => pat.test((b.textContent||'').replace(/\\s/g,'')) && visible(b));
    if (btn) { btn.click(); return {ok:true,sent:true,method:'btn',text:btn.textContent.trim()}; }
  }

  // Strategy 2: button near the input with send-related class
  if (container) {
    const nearby = container.querySelectorAll('button, [class*="send"], [class*="btn-send"], [class*="send-btn"]');
    for (const btn of nearby) { if (visible(btn)) { btn.click(); return {ok:true,sent:true,method:'nearby'}; } }
  }

  // Strategy 3: any short-text button with 发/送
  const fb = allBtns.find(b => { const t=(b.textContent||'').replace(/\\s/g,''); return (t.includes('发')||t.includes('送')) && t.length<=8 && visible(b); });
  if (fb) { fb.click(); return {ok:true,sent:true,method:'fuzzy',text:fb.textContent.trim()}; }

  // Strategy 4: any icon-only button (SVG) in the chat footer area
  if (container) {
    const iconBtns = container.querySelectorAll('button');
    for (const btn of iconBtns) {
      if (!visible(btn)) continue;
      const hasIcon = btn.querySelector('svg, img, i, [class*="icon"]');
      const hasNoText = !(btn.textContent||'').trim();
      if (hasIcon || hasNoText) { btn.click(); return {ok:true,sent:true,method:'icon'}; }
    }
  }

  // Strategy 5: Enter key
  input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true}));
  input.dispatchEvent(new KeyboardEvent('keypress',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true}));
  input.dispatchEvent(new KeyboardEvent('keyup',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true}));

  // Strategy 6: Ctrl+Enter (some chat UIs)
  input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,ctrlKey:true,bubbles:true,cancelable:true}));

  return {ok:true,sent:true,method:'enter'};
})()
"""

def _fill_only_js(msg: str) -> str:
    m = json.dumps(msg, ensure_ascii=False)
    return """( () => {
  const msg = """ + m + """;
  // Search inside dialogs/chats first (same logic as WAIT_FOR_CHAT_INPUT_JS)
  const cs = document.querySelectorAll('[class*="dialog"], [class*="chat"], [class*="modal"], [class*="drawer"], body');
  let input = null;
  for (const c of cs) { if (!c.offsetParent) continue; input = c.querySelector('textarea, [contenteditable="true"], [role="textbox"]'); if (input && input.offsetParent) break; }
  if (!input) return false;
  input.focus();
  if (input.isContentEditable || input.getAttribute('contenteditable')==='true') {
    input.textContent = msg;
    input.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:msg}));
  } else {
    const s = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value') || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
    if(s?.set) s.set.call(input, msg); else input.value = msg;
    input.dispatchEvent(new Event('input',{bubbles:true}));
    input.dispatchEvent(new Event('change',{bubbles:true}));
  }
  return true;
})()
"""


class AutomationEngine:

    def __init__(self):
        self._running = False
        self._status = "idle"
        self._stats: dict[str, int] = {"sent": 0, "skipped": 0, "errors": 0, "total": 0}
        self._chat_count = 0
        self._consecutive = 0
        self._on_progress_cb = None
        self._login_watchdog_task = None
        self._mode = "expected"
        self._run_done = None  # lazily created in run()

    @property
    def running(self) -> bool: return self._running
    @property
    def status(self) -> str: return self._status
    @property
    def stats(self) -> dict: return dict(self._stats)

    async def run(self, settings: dict, resume_analysis: dict, on_progress=None, already_sent: set = None, batch_id: str = "", mode: str = "expected", search_keyword: str = "") -> dict:
        # Wait for any previous run to fully exit before starting a new one
        if self._run_done is None:
            self._run_done = asyncio.Event()
            self._run_done.set()
        await self._run_done.wait()
        self._run_done.clear()
        self._running = True
        self._on_progress_cb = on_progress
        self._mode = mode
        self._stats = {"sent": 0, "skipped": 0, "errors": 0, "total": 0}
        self._chat_count = 0
        self._consecutive = 0

        # Compute job tab from settings
        keyword = settings.get("target_job_keyword", "产品经理")
        cities = settings.get("target_cities", ["重庆"])
        city = cities[0] if cities else "重庆"
        self._job_tab = f"{keyword}({city})"
        self._job_keyword = keyword
        filter_city = settings.get("filter_city") or city
        self._filter_city = filter_city

        try:
            bm = get_browser()
            if not bm.running:
                self._emit("error", "浏览器未运行", on_progress)
                return self._result(False, "no_browser")

            daily_limit = min(int(settings.get("daily_chat_limit", 50)), HARD_DAILY_LIMIT)
            min_score = int(settings.get("min_score_to_chat", 72))
            cooldown_min = max(int(settings.get("cooldown_min_ms", 9000)), 5000)
            cooldown_max = max(int(settings.get("cooldown_max_ms", 18000)), cooldown_min + 5000)
            auto_send = bool(settings.get("auto_send_initial", True))
            stop_on_risk = bool(settings.get("stop_on_risk_prompt", True))

            processed: set[str] = set()

            # ── 优先处理已评分 / 异常岗位 ──────────────────────
            from app.database import SessionLocal as _PrioritySL
            from app.models import Job as _PriorityJob
            _pdb = _PrioritySL()
            try:
                _pending_jobs = _pdb.query(_PriorityJob).filter(
                    _PriorityJob.status.in_(["evaluated", "error"]),
                    _PriorityJob.url != ""
                ).order_by(_PriorityJob.seq).all()
                _pdb.close()
                _pdb = None
                if _pending_jobs:
                    _priority_total = len(_pending_jobs)
                    self._stats["total"] = _priority_total
                    self._emit(
                        "priority",
                        f"开始优先处理 {_priority_total} 个已评分/异常岗位",
                        on_progress,
                    )
                    _priority_idx = 0
                    for _pj in _pending_jobs:
                        if not self._running:
                            break
                        if self._chat_count >= daily_limit:
                            self._emit("paused", f"达上限 {daily_limit}", on_progress)
                            self._running = False
                            break
                        sk = _pj.source_key or (_pj.url or "").split("?")[0]
                        if sk in processed:
                            continue
                        processed.add(sk)
                        _priority_idx += 1
                        _card = {
                            "url": _pj.url,
                            "source_key": sk,
                            "title": _pj.title,
                            "company": _pj.company,
                            "salary": _pj.salary,
                            "city": _pj.city,
                            "description": _pj.description,
                        }
                        self._stats["sent"] = self._stats.get("sent", 0)
                        self._stats["skipped"] = self._stats.get("skipped", 0)
                        self._stats["errors"] = self._stats.get("errors", 0)
                        label = f"优先 {_priority_idx}/{_priority_total}"
                        _pre_eval = {
                            "score": _pj.score,
                            "decision": _pj.decision or "contact",
                            "status": "evaluated",
                            "reasons": list(_pj.reasons or []),
                            "risks": list(_pj.risks or []),
                            "initial_message": _pj.initial_message or "",
                        }
                        try:
                            _msg = await self._process_one(
                                bm, _pj.url, settings, resume_analysis,
                                min_score, auto_send,
                                idx=_priority_idx - 1,
                                batch_id=batch_id,
                                on_progress=on_progress,
                                job_card=_card,
                                pre_eval=_pre_eval,
                            )
                            self._emit("running", f"[{label}] {_msg}", on_progress)
                        except Exception as _exc:
                            self._stats["errors"] += 1
                            self._emit("running", f"[{label}] 异常: {_exc}", on_progress)
                        delay = random.randint(cooldown_min, cooldown_max) / 1000
                        await asyncio.sleep(delay)
                    self._stats["total"] = self._stats.get("sent", 0) + self._stats.get("skipped", 0) + self._stats.get("errors", 0)
                    self._emit(
                        "completed",
                        f"优先处理完成 — 发送 {self._stats['sent']}，跳过 {self._stats['skipped']}，错误 {self._stats['errors']}",
                        on_progress,
                    )
                if not self._running:
                    return self._result(True, f"优先处理完成 — 发送 {self._stats['sent']}，跳过 {self._stats['skipped']}")
            finally:
                if _pdb:
                    _pdb.close()


            # 根据模式选择列表来源
            if mode == "search":
                self._emit("selecting", f"搜索岗位：{search_keyword}", on_progress)
                selected = await self._prepare_search_list(bm, search_keyword)
                if not selected.get("ok"):
                    self._emit("error", f"搜索失败: {selected.get('reason', selected)}", on_progress)
                    return self._result(False, "搜索失败")
                self._emit("selecting", f"搜索结果：{selected.get('cardCount', 0)} 个岗位", on_progress)
            elif mode == "recommend":
                self._emit("selecting", "使用推荐页面", on_progress)
                selected = await self._prepare_recommend_list(bm)
                if not selected.get("ok"):
                    self._emit("error", "推荐页面加载失败", on_progress)
                    return self._result(False, "推荐页加载失败")
                self._emit("selecting", f"推荐页面：{selected.get('cardCount', 0)} 个岗位", on_progress)
            else:
                self._emit("selecting", f"定位岗位列表：{self._job_tab}", on_progress)
                selected = await self._prepare_target_job_list(bm)
                if not selected.get("ok"):
                    message = f"岗位推荐页未加载成功，未找到岗位卡片"
                    self._emit("error", message, on_progress)
                    return self._result(False, message)
                self._emit(
                    "selecting",
                    f"已锁定 {self._job_tab}，可见岗位 {selected.get('cardCount', 0)} 个",
                    on_progress,
                )
            await asyncio.sleep(1)

            empty_rounds = 0
            stop_all = False
            self._emit("extracting", "提取岗位列表...", on_progress)

            self._start_login_watchdog(bm, on_progress)

            while self._running and not stop_all:
                # Navigate back to list page for next batch (we may be on a detail/chat page)
                nav_ok = False
                for nav_retry in range(3):
                    try:
                        current_list_url = await bm.current_url()
                        if "/web/geek/jobs" not in current_list_url:
                            list_url = "https://www.zhipin.com/web/geek/jobs"
                            await bm.navigate(list_url)
                            await asyncio.sleep(2)
                            if await self._check_page_risk(bm, stop_on_risk, on_progress):
                                self._running = False
                                self._status = "risk"
                                self._emit("stopped", f"风控停止 — 已发送 {self._stats['sent']}，跳过 {self._stats['skipped']}", on_progress)
                                stop_all = True
                                break
                        nav_ok = True
                        break
                    except Exception as e:
                        self._emit("running", f"导航恢复中({nav_retry+1}/3): {e}", on_progress)
                        await asyncio.sleep(3)
                if stop_all:
                    break
                if not nav_ok:
                    self._emit("error", "无法回到列表页，浏览器可能断开", on_progress)
                    self._stats["errors"] += 1
                    self._running = False
                    break
                if self._chat_count >= daily_limit:
                    self._emit("paused", f"达上限 {daily_limit}", on_progress)
                    break

                try:
                    jobs = await self._extract_jobs(bm)
                except Exception as e:
                    self._emit("running", f"提取失败，重试中: {e}", on_progress)
                    await asyncio.sleep(2)
                    jobs = await self._extract_jobs(bm)
                fresh_jobs = []
                for job_card in [j for j in jobs if j.get("url")]:
                    url = job_card["url"]
                    source_key = job_card.get("source_key") or url.split("?")[0]
                    if source_key in processed:
                        continue
                    processed.add(source_key)
                    if already_sent and source_key in already_sent:
                        title = (job_card.get("title") or "岗位")[:60]
                        self._emit("running", f"⏭ 已投递过，忽略统计 | {title}", on_progress)
                        continue
                    fresh_jobs.append(job_card)

                if not fresh_jobs:
                    if not jobs:
                        # 页面上真的一张卡片都没有
                        empty_rounds += 1
                        if empty_rounds > MAX_EMPTY_SCROLL_ROUNDS:
                            break
                        self._emit(
                            "loading",
                            f"继续向下加载岗位... ({empty_rounds}/{MAX_EMPTY_SCROLL_ROUNDS})",
                            on_progress,
                        )
                    await self._load_more_jobs(bm)
                    # 加载完成，直接重新提取（不走下一轮 while）
                    try:
                        await asyncio.sleep(1)
                        jobs = await self._extract_jobs(bm)
                        fresh_jobs = []
                        for job_card in [j for j in jobs if j.get("url")]:
                            url = job_card["url"]
                            source_key = job_card.get("source_key") or url.split("?")[0]
                            if source_key in processed:
                                continue
                            processed.add(source_key)
                            if already_sent and source_key in already_sent:
                                continue
                            fresh_jobs.append(job_card)
                    except Exception:
                        pass
                    if not fresh_jobs:
                        continue

                empty_rounds = 0
                self._emit(
                    "running",
                    f"发现 {len(fresh_jobs)} 个新岗位，累计待处理 {self._stats['total'] + len(fresh_jobs)} 个",
                    on_progress,
                )

                # Track batch total for progress denominator
                self._stats["total"] += len(fresh_jobs)

                batch_total = len(fresh_jobs)
                completed_in_batch = 0
                for job_card in fresh_jobs:
                    if not self._running:
                        stop_all = True
                        self._emit("running", f"[DEBUG] 批次中断：running=False，已处理{completed_in_batch}/{batch_total}，剩余{batch_total-completed_in_batch}个不计入total", on_progress)
                        self._stats["total"] -= (batch_total - completed_in_batch)
                        break
                    if self._chat_count >= daily_limit:
                        self._emit("paused", f"达上限 {daily_limit}", on_progress)
                        stop_all = True
                        self._emit("running", f"[DEBUG] 批次中断：达上限，已处理{completed_in_batch}/{batch_total}，剩余{batch_total-completed_in_batch}个不计入total", on_progress)
                        self._stats["total"] -= (batch_total - completed_in_batch)
                        break

                    url = job_card["url"]
                    idx = self._stats.get("sent", 0) + self._stats.get("skipped", 0) + self._stats.get("errors", 0)

                    if await self._check_page_risk(bm, stop_on_risk, on_progress):
                        self._stats["errors"] += 1
                        self._running = False
                        self._status = "risk"
                        self._emit("stopped", f"风控停止 — 已发送 {self._stats['sent']}，跳过 {self._stats['skipped']}", on_progress)
                        self._stats["total"] -= (batch_total - completed_in_batch - 1)
                        self._emit("running", f"[DEBUG] 批次中断：风控，已处理{completed_in_batch}/{batch_total}，剩余{batch_total-completed_in_batch-1}个不计入total", on_progress)
                        stop_all = True
                        break

                    try:
                        msg = await self._process_one(
                            bm,
                            url,
                            settings,
                            resume_analysis,
                            min_score,
                            auto_send,
                            idx,
                            batch_id,
                            on_progress,
                            job_card,
                        )
                        self._emit("running", msg, on_progress)
                    except Exception as exc:
                        self._stats["errors"] += 1
                        await self._record_job_result(
                            job_card,
                            {
                                "score": 0,
                                "decision": "review",
                                "status": "error",
                                "reasons": [f"处理异常: {exc}"],
                                "risks": [],
                                "initial_message": "",
                            },
                            batch_id,
                        )
                        self._emit("running", f"[{idx+1}] 异常: {exc}", on_progress)

                    completed_in_batch += 1

                    if not self._running:
                        stop_all = True
                        self._emit("running", f"[DEBUG] 批次中断：running=False(处理后)，已处理{completed_in_batch}/{batch_total}，剩余{batch_total-completed_in_batch}个不计入total", on_progress)
                        self._stats["total"] -= (batch_total - completed_in_batch)
                        break

                    if await self._check_page_risk(bm, stop_on_risk, on_progress):
                        self._stats["errors"] += 1
                        self._running = False
                        self._status = "risk"
                        self._emit("stopped", f"风控停止 — 已发送 {self._stats['sent']}，跳过 {self._stats['skipped']}", on_progress)
                        stop_all = True
                        break

                    delay = random.randint(cooldown_min, cooldown_max) / 1000
                    if self._consecutive >= LONG_BREAK_EVERY_N:
                        delay += random.randint(LONG_BREAK_MIN_SEC, LONG_BREAK_MAX_SEC)
                        self._consecutive = 0
                    self._emit("running", f"等待 {delay:.0f}s...", on_progress)
                    await asyncio.sleep(delay)

                if not stop_all:
                    await self._load_more_jobs(bm)

            if self._stats["total"] == 0:
                url = await bm.current_url()
                self._emit("error", f"未找到新的可处理岗位。当前页面: {url}", on_progress)
                return self._result(False, "no_jobs")

            # 根据退出原因选择正确的结束状态
            exit_reason = "empty"  # default: 岗位已穷尽
            if self._status == "risk":
                exit_reason = "risk"
            elif self._chat_count >= daily_limit:
                exit_reason = "daily_limit"
            self._emit("running", f"[DEBUG] exit_reason={exit_reason}, _running={self._running}, _status={self._status}, stop_all={stop_all}, total={self._stats.get('total',0)}, sent={self._stats.get('sent',0)}, skipped={self._stats.get('skipped',0)}, errors={self._stats.get('errors',0)}", on_progress)
            if exit_reason == "empty":
                msg = f"完成 — 发送 {self._stats['sent']}，跳过 {self._stats['skipped']}，错误 {self._stats['errors']}"
                self._emit("completed", msg, on_progress)
            elif exit_reason == "daily_limit":
                msg = f"达投递上限 {daily_limit} — 已发送 {self._stats['sent']}，跳过 {self._stats['skipped']}，错误 {self._stats['errors']}"
                self._emit("paused", msg, on_progress)
            elif exit_reason == "risk":
                msg = f"风控/登录失效停止 — 发送 {self._stats['sent']}，跳过 {self._stats['skipped']}，错误 {self._stats['errors']}"
                self._emit("stopped", msg, on_progress)
            return self._result(True, msg)
        except Exception as exc:
            self._emit("error", str(exc), on_progress)
            return self._result(False, str(exc))
        finally:
            self._running = False
            if self._run_done:
                self._run_done.set()
            if self._login_watchdog_task and not self._login_watchdog_task.done():
                self._login_watchdog_task.cancel()
            # Safety net: final progress update for unexpected stop paths
            if self._on_progress_cb and not (self._status or "").startswith("[completed]"):
                self._emit(self._status.split("]")[0].lstrip("[") if "]" in (self._status or "") else "stopped",
                           f"已停止 — 发送 {self._stats['sent']}，跳过 {self._stats['skipped']}，错误 {self._stats['errors']}",
                           self._on_progress_cb)

    async def _record_job_result(self, job_info: dict, evaluation: dict, batch_id: str = "") -> bool:
        try:
            import json as _json, urllib.request as _req

            payload = {"job": job_info, "resume_id": None, "evaluation": evaluation}
            if batch_id:
                payload["batch_id"] = batch_id
            data = _json.dumps(payload).encode()
            await asyncio.to_thread(
                lambda: _req.urlopen(
                    _req.Request(
                        "http://127.0.0.1:8788/api/jobs/evaluate",
                        data=data,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ),
                    timeout=10,
                ).read()
            )
            return True
        except Exception:
            return False

    async def _record_skipped_job(self, job_info: dict, reason: str, batch_id: str = "", *, score: int = 0) -> bool:
        return await self._record_job_result(job_info, _skip_evaluation(reason, score=score), batch_id)
    async def _process_one(
        self,
        bm,
        url,
        settings,
        resume,
        min_score,
        auto_send,
        idx,
        batch_id="",
        on_progress=None,
        job_card: Optional[dict] = None,
        pre_eval: Optional[dict] = None,
    ) -> str:
        from app.services.deepseek import evaluate_job, generate_initial_message

        job_card = job_card or {"url": url, "source_key": url.split("?")[0]}
        title = (job_card.get("title") or "?")[:60]

        # Step 1: Score (skip AI if priority pre_eval provided)
        if pre_eval:
            eval_result = dict(pre_eval)
        else:
            await self._random_delay(600, 1800)
            try:
                eval_result = await evaluate_job(resume, job_card, settings)
            except Exception as e:
                await self._record_job_result(job_card, {
                    "score": 0, "decision": "review",
                    "status": "error",
                    "reasons": [f"AI评估异常: {e}"],
                    "risks": [], "initial_message": "",
                }, batch_id)
                self._stats["errors"] += 1
                return f"[{idx+1}] 评估异常(error): {e}"
        score = int(eval_result.get("score") or 0)

        if eval_result.get("decision") == "skip" or score < min_score:
            reason = f"分数 {score} 低于开聊线 {min_score}" if score < min_score else "AI判断不适合"
            eval_result = _mark_evaluation_skipped(eval_result, reason)
            await self._record_job_result(job_card, eval_result, batch_id)
            self._emit("evaluated", f"[{idx+1}] 已评分 {score}分，跳过 | {title}", on_progress)
            await asyncio.sleep(1)
            self._stats["skipped"] += 1
            return f"[{idx+1}] 跳过 {score}分 | {title}"

        eval_result["status"] = "evaluated"
        await self._record_job_result(job_card, eval_result, batch_id)
        self._emit("evaluated", f"[{idx+1}] 已评分 {score}分，准备开聊 | {title}", on_progress)
        await asyncio.sleep(1)

        # Step 2: Open detail page in new tab
        detail_page = None
        try:
            detail_page, is_login = await self._safe_open_tab(bm, url, on_progress)
            if is_login:
                await self._record_job_result(job_card, {
                    "score": eval_result.get("score", 0),
                    "decision": "review",
                    "status": "error",
                    "reasons": ["登录失效，无法打开详情页"],
                    "risks": [],
                    "initial_message": eval_result.get("initial_message", ""),
                }, batch_id)
                self._stats["errors"] += 1
                return f"[{idx+1}] \u26a0\ufe0f 登录失效 | {title}"
            if not detail_page:
                eval_result = _mark_evaluation_skipped(eval_result, "打开标签页失败")
                await self._record_job_result(job_card, eval_result, batch_id)
                self._stats["skipped"] += 1
                return f"[{idx+1}] 打开失败 | {title}"
            await asyncio.sleep(2)

            # Verify on detail page
            verify = await bm.evaluate_on(detail_page, """(() => {
              return {ok: location.href.includes('job_detail'), url: location.href};
            })()""")
            if not verify.get("ok"):
                eval_result = _mark_evaluation_skipped(eval_result, "未进入详情页")
                await self._record_job_result(job_card, eval_result, batch_id)
                self._stats["skipped"] += 1
                await bm.close_tab(detail_page)
                return f"[{idx+1}] 未进入详情页 | {title}"

            # Step 3: Click "立即沟通" (first click — opens dialog, button changes to "继续沟通")
            btn1 = await bm.evaluate_on(detail_page, """(() => {
              const b = document.querySelector('a.btn-startchat');
              if (!b || !b.offsetParent) return {found: false};
              b.click();
              return {found: true, text: b.textContent?.trim()};
            })()""")
            if not btn1.get("found"):
                eval_result = _mark_evaluation_skipped(eval_result, "未找到立即沟通按钮")
                await self._record_job_result(job_card, eval_result, batch_id)
                self._stats["skipped"] += 1
                await bm.close_tab(detail_page)
                return f"[{idx+1}] 无沟通按钮 | {title}"

            # Step 4: Click "继续沟通" (second click — navigates to /web/geek/chat)
            await asyncio.sleep(2)
            btn2 = await bm.evaluate_on(detail_page, """(() => {
              const b = document.querySelector('a.btn-startchat');
              if (!b || !b.offsetParent) return {found: false};
              if (!/继续/.test(b.textContent||'')) return {found: false, text: b.textContent?.trim()};
              b.click();
              return {found: true};
            })()""")
            if not btn2.get("found"):
                # Job was already contacted in a previous session
                await self._record_job_result(job_card, {
                    "score": eval_result.get("score", 0),
                    "decision": eval_result.get("decision", "contact"),
                    "status": "chat_started",
                    "reasons": list(eval_result.get("reasons", [])),
                    "risks": list(eval_result.get("risks", [])),
                    "initial_message": eval_result.get("initial_message", ""),
                }, batch_id)
                self._stats["sent"] += 1
                await bm.close_tab(detail_page)
                return f"[{idx+1}] 已沟通 | {title}"

            # Step 5: Wait for chat page (same tab, URL changes to /chat)
            await asyncio.sleep(3)
            chat_ready = False
            for _ in range(20):
                try:
                    if await bm.evaluate_on(detail_page, WAIT_FOR_CHAT_INPUT_JS):
                        chat_ready = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            if not chat_ready:
                eval_result = _mark_evaluation_skipped(eval_result, "聊天页未加载")
                await self._record_job_result(job_card, eval_result, batch_id)
                self._stats["skipped"] += 1
                await bm.close_tab(detail_page)
                return f"[{idx+1}] 聊天页未加载 | {title}"

            # Step 6: Get AI message
            message = eval_result.get("initial_message") or ""
            if not message:
                message = (await generate_initial_message(resume, job_card, settings)).get("message", "")
            if not message:
                eval_result = _mark_evaluation_skipped(eval_result, "未生成开场白")
                await self._record_job_result(job_card, eval_result, batch_id)
                self._stats["skipped"] += 1
                await bm.close_tab(detail_page)
                return f"[{idx+1}] 无话术 | {title}"

            if not auto_send:
                await self._random_delay(400, 800)
                await bm.evaluate_on(detail_page, _fill_only_js(message))
                return f"[{idx+1}] \U0001f4dd 已填入 | {title}"

            # Step 7: Type and send (逐字输入模拟)
            await self._random_delay(600, 1500)
            # Focus and clear input
            await bm.evaluate_on(detail_page, """(() => {
              const cs = document.querySelectorAll('[class*="dialog"], [class*="chat"], body');
              let input = null;
              for (const c of cs) { if (!c.offsetParent) continue; input = c.querySelector('textarea, [contenteditable="true"], [role="textbox"]'); if (input && input.offsetParent) break; }
              if (!input) return false;
              input.focus(); input.click();
              if (input.isContentEditable) input.textContent = '';
              else { input.value = ''; input.dispatchEvent(new Event('input', {bubbles:true})); }
              return true;
            })()""")
            await asyncio.sleep(0.3)
            await detail_page.bring_to_front()
            await asyncio.sleep(0.2)
            await detail_page.keyboard.type(message, delay=55)
            await asyncio.sleep(0.4)
            await detail_page.keyboard.press("Enter")
            await asyncio.sleep(2)

            self._stats["sent"] += 1
            self._chat_count += 1
            self._consecutive += 1
            try:
                import json as _json, urllib.request as _req
                data = _json.dumps({"type": "chat_started", "payload": {"source_key": job_card.get("source_key",""), "score": score}}).encode()
                await asyncio.to_thread(lambda: _req.urlopen(_req.Request(
                    "http://127.0.0.1:8788/api/events", data=data,
                    headers={"Content-Type":"application/json"}, method="POST"), timeout=5).read())
            except Exception:
                pass
            return f"[{idx+1}] ✅ 已发送 {score}分 | {title}"
        finally:
            if detail_page:
                await bm.close_tab(detail_page)

    async def _extract_jobs(self, bm) -> list[dict]:
        await asyncio.sleep(3)
        for attempt in range(3):
            try:
                result = await bm.evaluate(EXTRACT_JOB_LIST_JS)
                if isinstance(result, list) and len(result) > 0:
                    return result
                if attempt < 2:
                    await bm.evaluate(SCROLL_JOB_LIST_JS)
                    await asyncio.sleep(3)
            except Exception:
                pass
        return []

    async def _load_more_jobs(self, bm) -> None:
        """滚动岗位列表并等待新卡片加载，最多尝试 5 次。"""
        for attempt in range(5):
            try:
                before = await bm.evaluate("document.querySelectorAll('.job-card-box').length") or 0
                await bm.evaluate(SCROLL_JOB_LIST_JS)
                # 等待 BOSS 懒加载新卡片
                for _ in range(8):
                    await asyncio.sleep(1)
                    after = await bm.evaluate("document.querySelectorAll('.job-card-box').length") or 0
                    if after > before:
                        return  # 新卡片已出现
                # 没出现新卡片，再试一次滚动
            except Exception:
                await asyncio.sleep(2)

    def _search_job_list_js(self, keyword: str) -> str:
        return SEARCH_AND_SUBMIT_JS.replace('"KEYWORD_PLACEHOLDER"', json.dumps(keyword, ensure_ascii=False))

    async def _prepare_search_list(self, bm, keyword: str) -> dict:
        """Fill search box + press Enter + wait for results."""
        await bm.navigate(TARGET_JOBS_URL)
        await asyncio.sleep(4)
        result = await bm.evaluate(self._search_job_list_js(keyword))
        if not (isinstance(result, dict) and result.get("ok")):
            return {"ok": False, "reason": "search_input_failed", "detail": result}
        await asyncio.sleep(0.5)
        await bm.press_enter()
        await asyncio.sleep(5)
        for _ in range(10):
            cards = await bm.evaluate("Array.from(document.querySelectorAll('.job-card-box')).filter(el => !!el.offsetParent).length")
            if cards > 0: break
            await asyncio.sleep(1)
        cards = await bm.evaluate("Array.from(document.querySelectorAll('.job-card-box')).filter(el => !!el.offsetParent).length")
        return {"ok": cards > 0, "cardCount": cards, "mode": "search"}

    async def _prepare_recommend_list(self, bm) -> dict:
        """Navigate to BOSS homepage/jobs page without query filters to get the recommend stream."""
        await bm.navigate("https://www.zhipin.com/web/geek/jobs")
        await asyncio.sleep(5)
        cards = await bm.evaluate("Array.from(document.querySelectorAll('.job-card-box, .job-card, .recommend-job-card, .job-list-item')).filter(el => !!el.offsetParent).length")
        return {"ok": cards > 0, "cardCount": cards, "url": await bm.current_url()}

    async def _prepare_target_job_list(self, bm) -> dict:
        """Navigate to jobs page, click expected job tab, select filter city."""
        await bm.navigate("https://www.zhipin.com/web/geek/jobs")
        await asyncio.sleep(4)
        # Click the "expected job" filter tab (e.g., "产品经理(重庆)")
        tab = await self._select_recommended_job_tab(bm, self._job_tab)
        await asyncio.sleep(3)
        if not tab.get("ok"):
            # Tab click failed — try once more after city select
            pass
        # Click the filter city
        await self._select_target_city(bm, self._filter_city)
        await asyncio.sleep(3)
        # If tab failed first time, retry after city is set
        if not tab.get("ok"):
            tab = await self._select_recommended_job_tab(bm, self._job_tab)
            await asyncio.sleep(3)
        cards = await bm.evaluate(
            "Array.from(document.querySelectorAll('.job-card-box, .job-card, .job-list-item, .recommend-job-card')).filter(el => !!el.offsetParent).length"
        )
        return {"ok": cards > 0, "cardCount": cards, "tabOk": tab.get("ok"), "url": await bm.current_url()}

    async def _target_list_state(self, bm) -> dict:
        try:
            result = await bm.evaluate(_target_list_state_js(self._job_tab, self._job_keyword))
            return result if isinstance(result, dict) else {"ok": False, "reason": "invalid_result"}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    async def _select_recommended_job_tab(self, bm, label: str) -> dict:
        try:
            current = await bm.current_url()
            if "zhipin.com" not in current:
                return {"ok": False, "reason": "not_boss_page", "url": current}
            result = await bm.evaluate(_select_recommended_job_tab_js(label))
            return result if isinstance(result, dict) else {"ok": False, "reason": "invalid_result"}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    async def _select_target_city(self, bm, city: str = DEFAULT_CITY) -> dict:
        try:
            state = await self._target_list_state(bm)
            if state.get("citySelected"):
                return {"ok": True, "already": True, "city": state.get("cityLabel") or city}
            opened = await bm.evaluate(_open_city_dialog_js())
            await asyncio.sleep(1)
            selected = await bm.evaluate(_select_city_option_js(city))
            if not (isinstance(selected, dict) and selected.get("ok")):
                opened = await bm.evaluate(_open_city_dialog_js())
                await asyncio.sleep(2)
                selected = await bm.evaluate(_select_city_option_js(city))
            return {
                "ok": bool(isinstance(selected, dict) and selected.get("ok")),
                "opened": opened,
                "selected": selected,
            }
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    async def _random_delay(self, a, b):
        await asyncio.sleep(random.randint(a, b) / 1000)

    def _emit(self, status, msg, cb):
        self._status = f"[{status}] {msg}"
        if cb:
            try: cb({"status": status, "message": msg, "stats": self._stats})
            except: pass

    def _result(self, ok, msg):
        return {"ok": ok, "message": msg, "stats": self._stats, "status": self._status}

    async def _check_page_risk(self, bm, stop_on_risk: bool, on_progress=None) -> bool:
        """Check current page for risk triggers. Returns True if should stop."""
        if not stop_on_risk:
            return False
        try:
            body_text = await bm.evaluate("document.body.innerText")
            if isinstance(body_text, str) and RISK_PATTERNS.search(body_text):
                match = RISK_PATTERNS.search(body_text).group(0)
                self._emit("risk", f"检测到风控: {match}，停止自动化", on_progress)
                return True
        except Exception:
            pass
        return False

    async def _safe_open_tab(self, bm, url: str, on_progress=None):
        """Open new tab, check URL + page content for login redirect."""
        try:
            page = await bm.open_tab(url)
            await asyncio.sleep(4)
            page_url = await bm.evaluate_on(page, "location.href") or ""

            url_suspect = isinstance(page_url, str) and (
                LOGIN_URL_PATTERNS.search(page_url) or "/web/user/" in page_url
            )
            is_login = False
            if url_suspect:
                try:
                    detect = await bm.evaluate_on(page, LOGIN_PAGE_DETECT_JS)
                    if isinstance(detect, dict) and detect.get("hasLoginForm"):
                        is_login = True
                except Exception:
                    if isinstance(page_url, str) and LOGIN_URL_PATTERNS.search(page_url):
                        is_login = True

            if is_login:
                self._emit("risk", "检测到登录失效，页面跳转至登录页，停止自动化", on_progress)
                self._running = False
                self._status = "risk"
                if self._on_progress_cb:
                    self._emit("stopped", f"登录失效停止 — 已发送 {self._stats['sent']}，跳过 {self._stats['skipped']}", self._on_progress_cb)
                try:
                    await bm.close_tab(page)
                except Exception:
                    pass
                return None, True
            return page, False
        except Exception:
            return None, False

    async def _check_all_pages_for_login(self, bm, on_progress=None) -> bool:
        """Check ALL open browser tabs for login URLs via CDP HTTP API.
        Does NOT depend on Playwright context — always works."""
        try:
            urls = bm.list_tab_urls()
            for url in urls:
                if isinstance(url, str) and LOGIN_URL_PATTERNS.search(url):
                    self._emit("risk", f"检测到登录失效: {url[:80]}，停止自动化", on_progress)
                    self._running = False
                    self._status = "risk"
                    if self._on_progress_cb:
                        self._emit("stopped", f"登录失效停止 — 已发送 {self._stats['sent']}，跳过 {self._stats['skipped']}", self._on_progress_cb)
                    return True
        except Exception:
            pass
        return False

    def _start_login_watchdog(self, bm, on_progress=None):
        """Spawn a background task that polls all tabs for login every 5 seconds."""
        if self._login_watchdog_task and not self._login_watchdog_task.done():
            return

        async def _poll():
            while self._running:
                await asyncio.sleep(5)
                try:
                    if await self._check_all_pages_for_login(bm, on_progress):
                        break
                except Exception:
                    pass

        self._login_watchdog_task = asyncio.create_task(_poll())

    def stop(self):
        was_running = self._running
        self._running = False
        if self._run_done:
            self._run_done.set()
        if self._login_watchdog_task and not self._login_watchdog_task.done():
            self._login_watchdog_task.cancel()
        if was_running and self._on_progress_cb:
            self._emit("stopped", f"任务已停止 — 发送 {self._stats['sent']}，跳过 {self._stats['skipped']}，错误 {self._stats['errors']}", self._on_progress_cb)


_engine: Optional[AutomationEngine] = None

def get_engine() -> AutomationEngine:
    global _engine
    if _engine is None:
        _engine = AutomationEngine()
    return _engine
