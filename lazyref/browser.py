"""Браузер без прямих мережевих дозволів; ресурси подає перевірений Python."""

import base64
import gzip
from io import BytesIO
import time
from weakref import WeakKeyDictionary
import zlib

from .access import AccessPolicy
from .models import check_cancel
from .network import Network
from .urls import remote_url


MAX_DECODED_BYTES = 20_000_000


class BackgroundBrowser:
    def __init__(self, cancel, access=None):
        self.cancel = cancel
        self.access = access or AccessPolicy()
        self.runtime = None
        self.browser = None
        self.sandbox = None
        self.network = Network(cancel)
        self.network.access = self.access
        self._document_errors = WeakKeyDictionary()
        self._page_attachments = WeakKeyDictionary()
        self._context_attachments = WeakKeyDictionary()

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        finally:
            self.browser = None
            self._document_errors.clear()
            self._page_attachments.clear()
            self._context_attachments.clear()
            self.network.close()
            try:
                if self.runtime:
                    self.runtime.stop()
            finally:
                self.runtime = None
                if self.sandbox:
                    self.sandbox.close()
                    self.sandbox = None

    def _start(self, deadline=None):
        from playwright.sync_api import sync_playwright
        from .browser_sandbox import BrowserSandbox
        check_cancel(self.cancel)
        if self.browser:
            return
        self.runtime = sync_playwright().start()
        try:
            self.sandbox = BrowserSandbox()
            self.browser = self.sandbox.launch(self.runtime.chromium, timeout_ms=self._remaining(deadline))
        except Exception as exc:
            try:
                self.close()
            except Exception as cleanup:
                exc.add_note("Очищення невдалого браузерного запуску: " + str(cleanup))
            raise RuntimeError("Не вдалося запустити ізольований браузер для запасного скріншота. " + str(exc)) from exc

    @staticmethod
    def _remaining(deadline):
        seconds = 20 if deadline is None else deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Вичерпано час запасного скріншота.")
        return max(1, int(seconds * 1000))

    def _response_parts(self, resource, deadline, method="GET"):
        headers = list(resource.header_items or resource.headers.items())
        encodings = [part.strip().lower() for name, value in headers
                     if name.lower() == "content-encoding" for part in value.split(",")]
        if len(encodings) > 4:
            raise ValueError("Забагато послідовних кодувань HTTP-відповіді.")
        body = resource.body
        for encoding in reversed(encodings):
            check_cancel(self.cancel)
            self._remaining(deadline)
            if encoding not in {"identity", "gzip", "x-gzip", "deflate"}:
                raise ValueError("Непідтримуване кодування HTTP-відповіді.")
            if encoding == "identity" or (not body and (method == "HEAD" or resource.status in {204, 304})):
                continue
            if not body:
                raise ValueError("Некоректна або неповна стиснена HTTP-відповідь.")
            try:
                if encoding in {"gzip", "x-gzip"}:
                    with gzip.GzipFile(fileobj=BytesIO(body)) as stream:
                        body = stream.read(MAX_DECODED_BYTES + 1)
                else:
                    decoder = zlib.decompressobj()
                    body = decoder.decompress(body, MAX_DECODED_BYTES + 1)
                    if len(body) <= MAX_DECODED_BYTES and (not decoder.eof or decoder.unused_data):
                        raise ValueError("Некоректна або неповна стиснена HTTP-відповідь.")
            except (OSError, EOFError, zlib.error) as exc:
                raise ValueError("Некоректна або неповна стиснена HTTP-відповідь.") from exc
            if len(body) > MAX_DECODED_BYTES:
                raise ValueError("Розпакований файл перевищує допустимий розмір.")
        check_cancel(self.cancel)
        self._remaining(deadline)
        if encodings and "html" in resource.content_type.lower():
            self.access.check_text(resource.url, body[:65536].decode("utf-8", errors="replace"))
        # Fetch.fulfillRequest передає готове тіло парсеру, без HTTP-декодера.
        # Зберігаємо CSP/CORS та інші повторні заголовки, але вже без стискання.
        headers = [(name, value) for name, value in headers
                   if name.lower() not in {"content-encoding", "content-length"}]
        if not any(name.lower() == "content-type" for name, _ in headers):
            headers.append(("Content-Type", resource.content_type))
        return body, headers

    def context(self, deadline=None):
        self._start(deadline)
        context = self.browser.new_context(viewport={"width": 1600, "height": 1100}, device_scale_factor=1,
            accept_downloads=False, service_workers="block", offline=False, java_script_enabled=True)
        # AppContainer є мережевою межею для всього дерева Edge, включно з
        # workers/WebRTC. Сторінка бачить navigator.onLine=true, а ресурси
        # отримує через перевірений транспорт. WebSocket додатково заблоковано.
        context.route_web_socket("**/*", lambda websocket: websocket.close())
        def attach_page(page):
            if page in self._page_attachments:
                return
            state = {"ready": False, "error": None}
            self._page_attachments[page] = state
            try:
                check_cancel(self.cancel)
                self._remaining(deadline)
                session = context.new_cdp_session(page)
                main_frame = session.send("Page.getFrameTree")["frameTree"]["frame"]["id"]

                def request_paused(event):
                    request_id = event["requestId"]
                    try:
                        check_cancel(self.cancel)
                        self._remaining(deadline)
                        request = event["request"]
                        url = remote_url(request["url"])
                        self.access.check(url)
                        resource = self.network.fetch_browser(url, method=request["method"],
                            headers=request.get("headers", {}), data=request.get("postData"),
                            deadline=deadline)
                        check_cancel(self.cancel)
                        self._remaining(deadline)
                        body, headers = self._response_parts(resource, deadline, request["method"])
                        session.send("Fetch.fulfillRequest", {
                            "requestId": request_id,
                            "responseCode": resource.status,
                            "responseHeaders": [{"name": name, "value": value}
                                                for name, value in headers],
                            "body": base64.b64encode(body).decode("ascii"),
                        })
                    except Exception as exc:
                        if event.get("resourceType") == "Document" and event.get("frameId") == main_frame:
                            self._document_errors.setdefault(page, exc)
                        # Закриття сторінки або спільний дедлайн можуть обірвати
                        # CDP під час відповіді; повторна помилка не маскує причину.
                        try:
                            session.send("Fetch.failRequest", {
                                "requestId": request_id, "errorReason": "Aborted"})
                        except Exception:
                            pass

                session.on("Fetch.requestPaused", request_paused)
                # Playwright context.route автоматично продовжує наступні URL
                # редиректу. CDP Fetch дає Python перевірити кожен окремий крок.
                # Workers/OOPIF, що не належать цьому CDP-сеансу, не мають
                # прямого доступу до мережі завдяки AppContainer.
                session.send("Fetch.enable", {
                    "patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
                state["ready"] = True
            except Exception as exc:
                state["error"] = exc
                try:
                    page.close()
                except Exception as cleanup:
                    exc.add_note("Закриття сторінки без перевіреного транспорту: " + str(cleanup))

        # Реєстрація перед new_page також охоплює подальші popup-сторінки.
        context.on("page", attach_page)
        self._context_attachments[context] = attach_page
        return context

    def navigate(self, page, url, deadline=None):
        check_cancel(self.cancel)
        self.access.check(url)
        url = remote_url(url)
        context = getattr(page, "context", None)
        attach = self._context_attachments.get(context) if context is not None else None
        if attach:
            attach(page)
        state = self._page_attachments.get(page)
        # У sync Playwright new_page може повернутися, поки обробник події page
        # ще очікує CDP. Навігація починається тільки після Fetch.enable ACK.
        while state is not None and not state["ready"]:
            check_cancel(self.cancel)
            remaining = self._remaining(deadline)
            if state["error"] is not None:
                raise state["error"]
            try:
                page.wait_for_timeout(min(10, remaining))
            except Exception:
                if state["error"] is not None:
                    raise state["error"]
                raise
        page.set_default_timeout(min(5000, self._remaining(deadline)))
        self._document_errors.pop(page, None)
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self._remaining(deadline))
        except Exception:
            check_cancel(self.cancel)
            self._remaining(deadline)
            error = self._document_errors.pop(page, None)
            if error:
                raise error
            raise
        check_cancel(self.cancel)
        self._remaining(deadline)
        error = self._document_errors.pop(page, None)
        if error:
            raise error
        self.access.check_page(page, response.status if response else None)

