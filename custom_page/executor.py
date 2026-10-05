"""
Custom career-page executor (Greenhouse, Lever, company ATS pages...).

How it works
1. A DOM scan finds every visible form control, works out its label and (for radios,
   dropdowns, checkbox groups) its real options, and tags each control with a
   data-flam-id so later fills hit exactly the right element.
2. Each question goes to the AnswerGenerator (in parallel). Choice questions are answered
   from the real options; unknown ones are flagged for the user.
3. Resume file inputs are filled with the user's saved resume PDF.
4. Submit is confirmed by page evidence (see executor/confirm.py), not assumed.

Limits (shown to the user as notes): multi-step portals (Workday-style), CAPTCHAs, and
custom widgets that are not real <input>/<select>/<textarea> elements.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from playwright.async_api import Browser, Page, Playwright, async_playwright

from executor.confirm import wait_for_confirmation
from telemetry.logger import log_event

if TYPE_CHECKING:
    from generator.answer_generator import AnswerGenerator
    from intake.post import PostInfo

logger = logging.getLogger(__name__)

RECEIPTS_DIR = Path(__file__).resolve().parent.parent / "receipts"
RECEIPTS_DIR.mkdir(exist_ok=True)

_CONSENT_RE = re.compile(r"\b(agree|terms|privacy|consent|policy|acknowledge|gdpr|i have read)\b", re.IGNORECASE)
_RESUME_RE = re.compile(r"\b(resume|cv|curriculum)\b", re.IGNORECASE)

_SCAN_JS = r"""
() => {
  const CTRL = "input, textarea, select";
  const txt = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  const shown = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const labelOf = (el) => {
    const al = el.getAttribute('aria-label'); if (al) return [al.trim(), 0.95];
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const s = lb.split(/\s+/).map(i => txt(document.getElementById(i))).join(' ').trim(); if (s) return [s, 0.95]; }
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l && txt(l)) return [txt(l), 0.95]; }
    const w = el.closest('label'); if (w && txt(w)) return [txt(w), 0.9];
    return ['', 0];
  };
  const groupLabel = (el) => {
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg && txt(lg)) return txt(lg); }
    const g = el.closest('[role=group],[role=radiogroup]');
    if (g) {
      const a = g.getAttribute('aria-label'); if (a) return a;
      const l = g.getAttribute('aria-labelledby'); if (l && document.getElementById(l)) return txt(document.getElementById(l));
    }
    // Climb only while the container holds nothing but this field's own inputs.
    const nm = el.getAttribute('name');
    const own = nm ? document.querySelectorAll('input[name="' + CSS.escape(nm) + '"]').length : 1;
    let n = el.parentElement;
    for (let i = 0; i < 4 && n; i++, n = n.parentElement) {
      if (n.querySelectorAll(CTRL).length > own) break;
      const h = n.querySelector('legend, h1, h2, h3, h4, [class*=label], [class*=question], [class*=title]');
      if (h && !h.contains(el) && txt(h) && txt(h).length < 200) return txt(h);
    }
    return '';
  };

  // Use the form that holds the most controls (skips newsletter boxes, search bars).
  let root = document, best = 0;
  for (const f of document.forms) {
    const n = [...f.querySelectorAll(CTRL)].filter(e => e.type === 'file' || shown(e)).length;
    if (n > best) { best = n; root = f; }
  }

  const groups = new Map();   // radio/checkbox groups by name
  const fields = [];
  let uid = 0;
  const SKIP_TYPES = new Set(['hidden', 'submit', 'button', 'image', 'reset', 'search']);

  for (const el of root.querySelectorAll(CTRL)) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'textarea' ? 'textarea' : tag === 'select' ? 'select' : 'text')).toLowerCase();
    if (SKIP_TYPES.has(type)) continue;
    if (el.closest('[aria-hidden=true]')) continue;
    const nm = (el.getAttribute('name') || el.id || '').toLowerCase();
    if (/honeypot|captcha|^hp_|trap|newsletter|search/.test(nm)) continue;

    const isChoice = type === 'radio' || type === 'checkbox';
    const lab = isChoice ? (el.closest('label') || (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]'))) : null;
    if (!(shown(el) || type === 'file' || (isChoice && shown(lab)))) continue;

    const id = String(uid++);
    el.setAttribute('data-flam-id', id);
    const required = el.hasAttribute('required') || el.getAttribute('aria-required') === 'true';

    if (isChoice) {
      const key = type + ':' + (el.getAttribute('name') || ('solo' + id));
      let g = groups.get(key);
      if (!g) {
        g = { field_type: type, label: '', confidence: 0, required, choices: [], ids: [], solo: !el.getAttribute('name') };
        groups.set(key, g); fields.push(g);
        const gl = groupLabel(el);
        if (gl) { g.label = gl; g.confidence = 0.9; }
      }
      let opt = lab ? txt(lab) : (el.getAttribute('aria-label') || el.value || '');
      g.choices.push(opt); g.ids.push(id);
      g.required = g.required || required;
      continue;
    }

    let [label, conf] = labelOf(el);
    if (!label && el.placeholder) { label = el.placeholder; conf = 0.7; }
    if (!label) { const gl = groupLabel(el); if (gl) { label = gl; conf = 0.7; } }
    if (!label && nm) { label = nm.replace(/[_\-\[\]]+/g, ' ').trim(); conf = 0.4; }

    let ft = 'short_text', choices = null;
    if (tag === 'textarea') ft = 'paragraph';
    else if (tag === 'select') {
      ft = 'dropdown';
      choices = [...el.options].map(o => txt(o)).filter(t => t && !/^(select|choose|please|--|—)/i.test(t));
    }
    else if (type === 'file') ft = 'file';
    else if (type === 'date') ft = 'date';
    else if (['datetime-local', 'time', 'month', 'week', 'color', 'range'].includes(type)) ft = 'unsupported';
    fields.push({ field_type: ft, label: label.slice(0, 200), confidence: conf || 0.2, required, choices, ids: [id], accept: el.getAttribute('accept') || '' });
  }

  // Single checkbox: its option text IS the question.
  for (const f of fields) {
    if (f.field_type === 'checkbox' && f.choices.length === 1 && (!f.label || f.solo)) { f.label = f.choices[0]; f.confidence = 0.9; }
    if ((f.field_type === 'radio' || f.field_type === 'checkbox') && !f.label) { f.label = f.choices.join(' / ').slice(0, 120); f.confidence = 0.3; }
  }

  // Submit button
  let submit = root.querySelector('button[type=submit], input[type=submit]');
  if (!submit) {
    const btns = [...root.querySelectorAll('button, [role=button], a.button')].filter(shown);
    submit = btns.reverse().find(b => /^\s*(submit|send|apply)/i.test(txt(b) || b.value || '')) || null;
  }
  if (submit) submit.setAttribute('data-flam-submit', '1');
  const hasNext = [...root.querySelectorAll('button, [role=button]')].some(b => shown(b) && /^\s*(next|continue)\b/i.test(txt(b)));
  const captcha = !!document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], .g-recaptcha, .h-captcha, [data-sitekey]');

  return { fields, submit: !!submit, hasNext, captcha };
}
"""


class CustomPageExecutor:
    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        logger.info("CustomPageExecutor started")

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("CustomPageExecutor stopped")

    # -----------------------------------------------------------------------
    # fill
    # -----------------------------------------------------------------------

    async def fill(
        self,
        page_url: str,
        generator: AnswerGenerator,
        jd_text: str = "",
        context: Optional[dict[str, str]] = None,
        user_template: Optional[str] = None,
        post: Optional["PostInfo"] = None,
        resume_path: Optional[str] = None,
    ) -> tuple[Page, list[dict], list[str]]:
        assert self._browser is not None, "Call start() first."
        page = await self._browser.new_page()
        try:
            logger.info("Navigating to career page: %s", page_url)
            await page.goto(page_url, wait_until="domcontentloaded", timeout=35_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass

            scan: dict[str, Any] = await page.evaluate(_SCAN_JS)
            fields: list[dict] = [f for f in scan["fields"] if f["field_type"] != "unsupported" or f["label"]]
            if not fields:
                raise RuntimeError(
                    "I couldn't find an application form on that page. It may be a job description page "
                    "(look for an 'Apply' button and send me that link) or a portal that needs a login."
                )
            log_event("custom_page_inspected", {"url": page_url, "fields_detected": len(fields)})

            notes: list[str] = []
            if scan["captcha"]:
                notes.append("This page has a CAPTCHA. Submitting may fail; you may need to apply by hand.")
            if scan["hasNext"] and not scan["submit"]:
                notes.append("This looks like a multi-step application. I can only see the first step.")
            elif not scan["submit"]:
                notes.append("I couldn't find a submit button on this page.")

            filled = await self._answer_fields(page, fields, generator, jd_text, context, user_template, post, resume_path)
            return page, filled, notes
        except Exception:
            await page.close()
            raise

    async def _answer_fields(self, page, fields, generator, jd_text, context, user_template, post, resume_path) -> list[dict]:
        # 1. which fields need the model
        jobs: list[tuple[int, Any]] = []
        entries: list[Optional[dict]] = [None] * len(fields)

        for i, f in enumerate(fields):
            label, ft = f["label"].strip(), f["field_type"]
            if not label:
                continue
            if ft == "file":
                entries[i] = await self._handle_file(page, f, resume_path)
            elif ft == "unsupported":
                entries[i] = _entry(f, None, "manual_required", True, "I can't fill this kind of field.")
            elif ft == "checkbox" and len(f["choices"]) == 1 and _CONSENT_RE.search(label):
                entries[i] = _entry(f, None, "manual_required", True, "Consent box. I never tick these for you.")
            else:
                jobs.append((i, generator.generate(
                    question=label, field_type=ft, jd_text=jd_text, context=context,
                    user_template=user_template, choices=f.get("choices") or None, post=post,
                )))

        # 2. generate in parallel, then fill in page order
        results = await asyncio.gather(*[j[1] for j in jobs])
        for (i, _), result in zip(jobs, results):
            f = fields[i]
            if result.value:
                try:
                    await self._apply(page, f, result.value)
                except Exception as exc:
                    logger.warning("Could not fill %r: %s", f["label"], exc)
                    result.flagged, result.note = True, "I couldn't put this in the form. Check it."
            entries[i] = _entry(f, result.value, result.source, result.flagged or f["confidence"] < 0.6,
                                result.note or ("I wasn't sure what this field is." if f["confidence"] < 0.6 else ""),
                                result.context_keys_used)
        return [e for e in entries if e]

    async def _handle_file(self, page: Page, f: dict, resume_path: Optional[str]) -> dict:
        label = f["label"] or "File upload"
        if _RESUME_RE.search(label) and resume_path and Path(resume_path).exists():
            try:
                await page.locator(f'[data-flam-id="{f["ids"][0]}"]').set_input_files(resume_path)
                return _entry(f, Path(resume_path).name, "profile", False, "", ["file:resume"])
            except Exception as exc:
                logger.warning("Resume upload failed: %s", exc)
        note = ("This form wants your resume as a file. I only have it as text, so send it once as a PDF with /upload."
                if _RESUME_RE.search(label) else "File upload. I can't attach this one.")
        return _entry({**f, "label": label}, None, "manual_required", True, note)

    # -----------------------------------------------------------------------
    # DOM actions
    # -----------------------------------------------------------------------

    @staticmethod
    def _pick(f: dict, wanted: str) -> Optional[int]:
        w = wanted.strip().lower()
        choices = [c.strip().lower() for c in f["choices"]]
        if w in choices:
            return choices.index(w)
        for i, c in enumerate(choices):
            if w in c or c in w:
                return i
        return None

    async def _apply(self, page: Page, f: dict, value: str) -> None:
        def loc(i: int):
            return page.locator(f'[data-flam-id="{f["ids"][i]}"]')

        ft = f["field_type"]
        if ft in ("short_text", "paragraph", "date"):
            await loc(0).fill(value)
        elif ft == "dropdown":
            idx = self._pick(f, value)
            if idx is None:
                raise RuntimeError(f"No option matches {value!r}")
            await loc(0).select_option(label=f["choices"][idx])
        elif ft in ("radio", "checkbox"):
            wanted = [v for v in value.split("|") if v.strip()] if ft == "checkbox" else [value]
            for w in wanted:
                idx = self._pick(f, w)
                if idx is None:
                    continue
                target = loc(idx)
                try:
                    await target.check(timeout=2_000)
                except Exception:
                    await target.evaluate("e => e.click()")  # styled inputs hidden behind a label

    async def refill(self, page: Page, field: dict, value: str) -> None:
        h = field.get("_handle")
        if not h:
            raise RuntimeError("That answer can't be changed in the live form.")
        await self._apply(page, h, value)

    # -----------------------------------------------------------------------
    # submit & cancel
    # -----------------------------------------------------------------------

    async def submit(self, page: Page, application_id: int) -> tuple[str, bool]:
        btn = page.locator('[data-flam-submit="1"]').first
        if not await btn.count():
            btn = page.get_by_role("button", name=re.compile(r"^\s*(submit|send|apply)", re.IGNORECASE)).last
        if not await btn.count():
            raise RuntimeError("Could not find the submit button on the career page.")

        await btn.scroll_into_view_if_needed()
        await btn.click()
        logger.info("Clicked submit for application %d", application_id)

        confirmed = await wait_for_confirmation(page)
        label = f"receipt_custom_{application_id}" if confirmed else f"unconfirmed_custom_{application_id}"
        receipt = await self._screenshot(page, label)
        await page.close()
        return receipt, confirmed

    async def cancel(self, page: Page) -> None:
        await page.close()

    async def _screenshot(self, page: Page, label: str) -> str:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = RECEIPTS_DIR / f"{label}_{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        return str(path)


def _entry(f: dict, value: Optional[str], source: str, flagged: bool, note: str = "",
           keys: Optional[list[str]] = None) -> dict:
    return {
        "question":          f["label"],
        "value":             value,
        "field_type":        f["field_type"],
        "skipped":           value is None,
        "flagged":           flagged,
        "confidence":        f.get("confidence", 1.0),
        "source":            source,
        "context_keys_used": keys or [],
        "note":              note,
        "_handle":           None if f["field_type"] in ("file", "unsupported") else f,
    }
