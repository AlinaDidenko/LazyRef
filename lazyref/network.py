"""Обмежені HTTP-запити: тайм-аути, розмір і перевірка адрес переходів."""

import ipaddress
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPSConnection
from http.cookiejar import CookieJar, DefaultCookiePolicy
import socket
import time
from threading import Event
from urllib.parse import urlencode, quote, urljoin, urlsplit, unquote
from urllib.error import HTTPError
from urllib.request import (Request, HTTPRedirectHandler, HTTPCookieProcessor, build_opener,
                            HTTPHandler, HTTPSHandler, ProxyHandler)

from .models import check_cancel
from .access import AccessPolicy, SourceBlocked, hostname
from .urls import remote_url


def public_url(url: str) -> str:
    url = remote_url(url)
    parts = urlsplit(url)
    _public_addresses(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    return url


def _public_addresses(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(info[4][0]).is_global for info in addresses):
        raise ValueError("Локальні й приватні мережеві адреси не завантажуються.")
    return addresses


def _connect_public(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    # Перевіряємо всі відповіді ДО першого socket.connect. До socket передається
    # числова адреса з цієї ж відповіді, тому другого DNS-запиту тут немає.
    addresses = _public_addresses(*address)
    error = None
    for family, kind, protocol, _, sockaddr in addresses:
        connection = socket.socket(family, kind, protocol)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError as exc:
            error = exc
            connection.close()
    raise error or OSError("Не вдалося підключитися до публічного джерела.")


class _PublicHTTPConnection(HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPSConnection(HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Стандартний HTTPSConnection зберігає початкове ім'я для Host,
        # SNI і перевірки TLS-сертифіката; змінюється лише TCP-підключення.
        self._create_connection = _connect_public


class _PublicHTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(_PublicHTTPConnection, request)


class _PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_PublicHTTPSConnection, request, context=self._context)


@dataclass(frozen=True)
class FetchedResource:
    url: str
    body: bytes
    content_type: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    header_items: tuple[tuple[str, str], ...] = ()


class _CookiePolicy(DefaultCookiePolicy):
    """Відкидаємо некоректні версії до стандартних перевірок домену."""

    def set_ok(self, cookie, request):
        return cookie.version in {0, 1} and super().set_ok(cookie, request)

    def return_ok(self, cookie, request):
        return cookie.version in {0, 1} and super().return_ok(cookie, request)


class Network:
    def __init__(self, cancel: Event, *, local_transport=False):
        self.cancel = cancel
        # Only the owned HTTP worker and explicit offline fixtures use local
        # I/O. Production callers never fall back to an uninterruptible socket.
        self._local_transport = local_transport
        self._download_process = None
        self._budget = ContextVar("lazyref_download_budget", default=None)
        self._request_serial = 0
        self.access = AccessPolicy()
        self.access_links: list[dict[str, str]] = []
        self.source_cache: dict = {}
        # Нова анонімна сесія на кожен пошук. Cookies браузера користувача не читаємо.
        self.cookies = CookieJar(policy=_CookiePolicy())
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        # Системний/ENV proxy міг би сам повторно резолвити вже перевірене ім'я.
        self.client = build_opener(ProxyHandler({}), _PublicHTTPHandler(),
                                  _PublicHTTPSHandler(), NoRedirect(), HTTPCookieProcessor(self.cookies))

    def close(self):
        try:
            if self._download_process is not None:
                self._download_process.close()
        finally:
            self.client.close()
            self.cookies.clear()
            self.source_cache.clear()

    @contextmanager
    def use_budget(self, budget=None):
        from .download_budget import DownloadBudget
        active = budget if budget is not None else self._budget.get()
        if active is None:
            active = DownloadBudget()
        token = self._budget.set(active)
        try:
            yield active
        finally:
            self._budget.reset(token)

    def cookie_value(self, name: str, url: str) -> str:
        request = Request(url)
        self.cookies.add_cookie_header(request)
        for item in request.get_header("Cookie", "").split("; "):
            key, _, value = item.partition("=")
            if key == name:
                return unquote(value)
        return ""

    def post_json(self, url: str, payload, *, headers=None):
        request_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        request_headers.update(headers or {})
        return json.loads(self._get(url, headers=request_headers,
                                    data=json.dumps(payload).encode("utf-8")))

    def get(self, url: str, *, params=None, headers=None, max_bytes=3_000_000) -> bytes:
        try:
            return self._get(url, params=params, headers=headers, max_bytes=max_bytes)
        except SourceBlocked as exc:
            # Перенаправлення на окремий CAPTCHA-домен блокує також початкове джерело.
            self.access.blocked[hostname(url)] = exc.reason
            raise

    def fetch(self, url: str, *, max_bytes=20_000_000) -> FetchedResource:
        """Браузер отримує лише кінцеву відповідь, ніколи необроблений редирект."""
        try:
            return self._get(url, max_bytes=max_bytes, metadata=True)
        except SourceBlocked as exc:
            self.access.blocked[hostname(url)] = exc.reason
            raise

    def fetch_browser(self, url: str, *, method="GET", headers=None, data=None,
                      deadline=None, max_bytes=20_000_000) -> FetchedResource:
        """Один HTTP-перехід: браузер зберігає URL модулів і правила CORS.

        Кожен наступний redirect знову потрапляє в цей транспорт. Власних
        підключень у браузера немає; DNS/TCP перевіряються перед підключенням.
        """
        if method not in {"GET", "HEAD", "OPTIONS"} or data is not None:
            raise ValueError("Фоновий перегляд дозволяє лише читання ресурсів.")
        check_cancel(self.cancel)
        self.access.check(url)
        url = remote_url(url)
        remaining = 20 if deadline is None else min(20, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("Вичерпано час запасного скріншота.")
        allowed_request = {"accept", "accept-language", "origin", "referer", "range",
                           "access-control-request-method", "access-control-request-headers", "user-agent"}
        request_headers = {k.lower(): v for k, v in (headers or {}).items() if k.lower() in allowed_request}
        request_headers.setdefault("user-agent", "LazyRef/0.7 (personal artist reference research)")
        request_headers["accept-encoding"] = "identity"
        try:
            response = self.client.open(Request(url, headers=request_headers, method=method), timeout=remaining)
        except HTTPError as exc:
            response = exc
        with response:
            status = response.status
            if status in {401, 403, 419, 429}:
                self.access.block(url, f"HTTP {status}: доступ обмежений")
            allowed_response = {"content-type", "content-encoding", "content-language", "content-range", "content-disposition",
                "accept-ranges", "location", "cache-control", "expires", "etag", "last-modified", "vary",
                "content-security-policy", "content-security-policy-report-only", "x-content-type-options",
                "x-frame-options", "referrer-policy", "permissions-policy", "cross-origin-resource-policy",
                "cross-origin-opener-policy", "cross-origin-embedder-policy", "access-control-allow-origin",
                "access-control-allow-credentials", "access-control-allow-methods", "access-control-allow-headers",
                "access-control-expose-headers", "access-control-max-age"}
            # Repeated CSP policies are cumulative; duplicate CORS headers may
            # intentionally be invalid. A dict alone would change both cases.
            response_items = [(k.lower(), v) for k, v in response.headers.items()
                              if k.lower() in allowed_response]
            response_headers = dict(response_items)
            if "location" in response_headers and status in {301, 302, 303, 307, 308}:
                if sum(name == "location" for name, _ in response_items) != 1:
                    raise ValueError("Неоднозначна адреса перенаправлення джерела.")
                response_headers["location"] = remote_url(urljoin(url, response_headers["location"]))
                self.access.check(response_headers["location"])
                response_items = [(name, response_headers["location"] if name == "location" else value)
                                  for name, value in response_items]
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise ValueError("Файл перевищує допустимий розмір.")
            body = bytearray()
            while method != "HEAD":
                check_cancel(self.cancel)
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Вичерпано час запасного скріншота.")
                chunk = response.read(65536)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ValueError("Файл перевищує допустимий розмір.")
            content_type = response.headers.get("content-type", "application/octet-stream")
            if "html" in content_type and not response.headers.get("content-encoding"):
                self.access.check_text(url, body[:65536].decode("utf-8", errors="replace"))
            check_cancel(self.cancel)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Вичерпано час запасного скріншота.")
            return FetchedResource(url, bytes(body), content_type, status, response_headers, tuple(response_items))

    def _get(self, url: str, *, params=None, headers=None, max_bytes=3_000_000, data=None, metadata=False):
        with self.use_budget() as budget:
            check_cancel(self.cancel)
            budget.check_direct()
            self.access.check(url)
            remote_url(url)
            if self._local_transport:
                self._request_serial += 1
                serial = self._request_serial
                budget_expired = False
                def progress(received, total, resource_id):
                    nonlocal budget_expired
                    check_cancel(self.cancel)
                    budget.observe(received, total, resource_id=f"{serial}:{resource_id}")
                    try:
                        budget.check_direct()
                    except TimeoutError:
                        budget_expired = True
                        raise
                try:
                    result = self._get_local(url, params=params, headers=headers, max_bytes=max_bytes,
                                             data=data, metadata=metadata, progress=progress)
                except TimeoutError as exc:
                    if budget_expired:
                        raise
                    raise OSError("Джерело надто довго не відповідає.") from exc
            else:
                from .download_process import DownloadProcess
                if self._download_process is None:
                    self._download_process = DownloadProcess(self.cancel, self.access, self.cookies)
                result = self._download_process.fetch(url, budget, params=params, headers=headers,
                                                      max_bytes=max_bytes, data=data)
                if not metadata:
                    result = result.body
            check_cancel(self.cancel)
            budget.check_direct()
            return result

    def _get_local(self, url: str, *, params=None, headers=None, max_bytes=3_000_000,
                   data=None, metadata=False, progress=None):
        """Verified HTTP transport; its owning process provides the hard deadline."""
        def report(received=0, total=None):
            check_cancel(self.cancel)
            if progress is not None:
                progress(received, total, str(hop))

        # params не переносяться повторно після редиректу.
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, quote_via=quote)
        for hop in range(6):
            report()
            self.access.check(url)
            url = remote_url(url)
            request_headers = {"User-Agent": "LazyRef/0.2 (personal artist reference research)"}
            request_headers.update(headers or {})
            try:
                response = self.client.open(Request(url, headers=request_headers, data=data), timeout=20)
            except HTTPError as exc:
                try:
                    report()
                except BaseException:
                    exc.close()
                    raise
                if exc.code in {301, 302, 303, 307, 308}:
                    if data is not None:
                        exc.close()
                        self.access.block(url, "перенаправлення API: потрібна перевірка доступу")
                    url = urljoin(url, exc.headers.get("location", ""))
                    # Не пересилаємо ключ постачальника на інший домен.
                    if urlsplit(url).hostname != urlsplit(exc.url).hostname:
                        headers = None
                    exc.close()
                    continue
                code = exc.code
                try:
                    if code in {401, 403, 419}:
                        self.access.block(url, f"HTTP {code}: доступ обмежений")
                    if code == 429:
                        self.access.block(url, "ліміт запитів HTTP 429")
                    if "html" in exc.headers.get("content-type", ""):
                        read = getattr(exc, "read1", exc.read)
                        error_body = bytearray()
                        while len(error_body) < 65536:
                            report(len(error_body))
                            chunk = read(65536 - len(error_body))
                            if not chunk:
                                break
                            error_body.extend(chunk)
                        self.access.check_text(url, error_body.decode("utf-8", errors="replace"))
                finally:
                    exc.close()
                raise RuntimeError(f"Джерело відповіло HTTP {code}.") from None
            with response:
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > max_bytes:
                    raise ValueError("Файл перевищує допустимий розмір.")
                total = int(length) if length.isdigit() else None
                report(0, total)
                chunks = bytearray()
                # HTTPResponse.read(n) can accumulate n bytes indefinitely on
                # a trickling source. read1 returns the currently available
                # bytes after at most one underlying read.
                read = getattr(response, "read1", response.read)
                while True:
                    report(len(chunks), total)
                    chunk = read(65536)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise ValueError("Файл перевищує допустимий розмір.")
                    report(len(chunks), total)
                if "html" in response.headers.get("content-type", ""):
                    self.access.check_text(url, chunks[:65536].decode("utf-8", errors="replace"))
                body = bytes(chunks)
                if metadata:
                    return FetchedResource(url, body, response.headers.get("content-type", "application/octet-stream"))
                return body
        raise ValueError("Забагато перенаправлень джерела.")

    def json(self, url: str, *, params=None, headers=None):
        import json
        return json.loads(self.get(url, params=params, headers=headers))

