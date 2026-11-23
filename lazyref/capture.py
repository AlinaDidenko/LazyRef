"""Запасний скріншот видимого фото, точно зіставленого з кандидатом."""

import sys
import time
from urllib.parse import unquote, urlsplit, parse_qsl

from playwright.sync_api import Error as BrowserError

from .models import Candidate, check_cancel
from .access import SourceBlocked, hostname
from .browser import BackgroundBrowser


PAGE_TIMEOUT_SECONDS = 20
POLL_SECONDS = 0.1
MAX_SCROLL_STEPS = 6


def remaining_ms(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Вичерпано час запасного скріншота. Переходжу до наступного кандидата.")
    return max(1, int(remaining * 1000))


IMAGE_STATE = """el => ({
    sources: [el.currentSrc, el.src, el.getAttribute('data-src'),
              el.getAttribute('data-lazy-src'), el.getAttribute('data-original')].map(src => {
        if (!src) return '';
        try { return new URL(src, document.baseURI).href; } catch (_) { return ''; }
    }),
    current: el.currentSrc || el.src,
    ready: el.complete && el.naturalWidth > 0 && el.naturalHeight > 0
})"""


def same_image(actual: str, expected: str) -> bool:
    if not actual or not expected:
        return False
    a, b = urlsplit(actual), urlsplit(expected)
    # Ігноруємо тільки розмір/трекінг. ?id=1 і ?id=2 можуть бути різними фото.
    def content_query(parts):
        ignored = {"w", "h", "width", "height", "size", "quality", "fit", "format", "auto"}
        return sorted((k, v) for k, v in parse_qsl(parts.query) if k.lower() not in ignored and not k.lower().startswith("utm_"))
    return (a.hostname == b.hostname and unquote(a.path) == unquote(b.path)
            and content_query(a) == content_query(b))


class BrowserCapture(BackgroundBrowser):
    def capture(self, candidate: Candidate, deadline=None) -> bytes:
        from .capture_process import CaptureProcess
        check_cancel(self.cancel)
        self.access.check(candidate.page_url)
        page_deadline = time.monotonic() + PAGE_TIMEOUT_SECONDS
        if deadline is not None:
            page_deadline = min(page_deadline, deadline)
        remaining_ms(page_deadline)
        if not hasattr(self, "_capture_process"):
            self._capture_process = CaptureProcess(self.cancel, self.access)
        return self._capture_process.capture(candidate, page_deadline)

    def close(self):
        try:
            if hasattr(self, "_capture_process"):
                self._capture_process.close()
        finally:
            super().close()

    def capture_local(self, candidate: Candidate, deadline=None) -> bytes:
        check_cancel(self.cancel)
        self.access.check(candidate.page_url)
        if deadline is None:
            deadline = time.monotonic() + PAGE_TIMEOUT_SECONDS
        remaining_ms(deadline)
        context = self.context(deadline=deadline)
        try:
            check_cancel(self.cancel)
            remaining_ms(deadline)
            page = context.new_page()
            self.navigate(page, candidate.page_url, deadline=deadline)
            return self.capture_element(page, candidate, deadline=deadline)
        except SourceBlocked as exc:
            self.access.blocked[hostname(candidate.page_url)] = exc.reason
            raise
        finally:
            primary = sys.exception()
            try:
                context.close()
            except Exception as cleanup:
                if primary is None:
                    raise
                primary.add_note("Не вдалося закрити контекст скріншота: " + str(cleanup))

    def capture_element(self, page, candidate: Candidate, deadline=None) -> bytes:
        """Чекаємо лише потрібне фото; завантаження сторінки не починає новий ліміт."""
        if deadline is None:
            deadline = time.monotonic() + PAGE_TIMEOUT_SECONDS
        next_access_check = 0
        next_scroll = 0
        scroll_steps = 0
        while True:
            check_cancel(self.cancel)
            remaining_ms(deadline)
            if time.monotonic() >= next_access_check:
                self.access.check_page(page)
                next_access_check = time.monotonic() + 0.5
            visible_match, obstructed = False, False
            images = page.locator("img")
            for i in range(min(images.count(), 150)):
                check_cancel(self.cancel)
                remaining_ms(deadline)
                node = images.nth(i)
                try:
                    state = node.evaluate(IMAGE_STATE, timeout=min(500, remaining_ms(deadline)))
                    if not any(same_image(src, expected) for src in state["sources"]
                               for expected in (candidate.image_url, candidate.thumbnail_url)):
                        continue
                    if not node.is_visible():
                        continue
                    visible_match = True
                    node.scroll_into_view_if_needed(timeout=min(500, remaining_ms(deadline)))
                    # Не очікуємо el.decode() без межі часу. Lazy-loading може
                    # замінити placeholder після прокрутки або додати img пізніше.
                    state = node.evaluate(IMAGE_STATE, timeout=min(500, remaining_ms(deadline)))
                    if not state["ready"] or not any(same_image(state["current"], expected)
                            for expected in (candidate.image_url, candidate.thumbnail_url)):
                        continue
                    box = node.bounding_box(timeout=min(500, remaining_ms(deadline)))
                    if not box or min(box["width"], box["height"]) < 180:
                        continue
                    # Виявляємо банери/модальні вікна над фотографією; їх не стираємо.
                    unobstructed = node.evaluate("""el => {
                        const r = el.getBoundingClientRect();
                        return [[.2,.2],[.8,.2],[.5,.5],[.2,.8],[.8,.8]].every(([x,y]) => {
                            const px=r.left+r.width*x, py=r.top+r.height*y;
                            if(px<0 || py<0 || px>=innerWidth || py>=innerHeight) return false;
                            const top=document.elementFromPoint(px,py);
                            return top===el || el.contains(top);
                        });
                    }""", timeout=min(500, remaining_ms(deadline)))
                    if unobstructed:
                        self.access.check_page(page)
                        check_cancel(self.cancel)
                        screenshot = node.screenshot(type="png", timeout=remaining_ms(deadline), animations="disabled")
                        check_cancel(self.cancel)
                        remaining_ms(deadline)
                        return screenshot
                    obstructed = True
                except BrowserError:
                    # SPA могла замінити вузол між двома операціями. Скасування
                    # та спільний дедлайн мають пріоритет над повторною спробою.
                    check_cancel(self.cancel)
                    remaining_ms(deadline)
                    if page.is_closed():
                        raise
            if obstructed:
                raise ValueError("Не знайшла видиму фотографію без перекриття. Пропускаю скріншот.")
            if not visible_match and scroll_steps < MAX_SCROLL_STEPS and time.monotonic() >= next_scroll:
                page.evaluate("() => window.scrollBy(0, Math.max(1, innerHeight * 0.8))")
                scroll_steps += 1
                next_scroll = time.monotonic() + 0.5
            page.wait_for_timeout(min(POLL_SECONDS * 1000, remaining_ms(deadline)))

