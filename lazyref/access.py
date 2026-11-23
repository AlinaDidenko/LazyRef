"""CAPTCHA означає пропустити джерело, а не просити користувача її розв'язувати."""

from html import unescape
import re
from urllib.parse import urlsplit


def hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


class SourceBlocked(RuntimeError):
    def __init__(self, url: str, reason: str):
        self.url = url
        self.host = hostname(url)
        self.reason = reason
        super().__init__(f"{self.host}: {reason}. Джерело пропущено.")


def gate_reason(url: str, text: str = "", title: str = "") -> str:
    host, path = hostname(url), urlsplit(url).path.lower()
    if (host == "google.com" or host.endswith(".google.com")) and path.startswith("/sorry"):
        return "CAPTCHA"
    if host in {"consent.google.com", "consent.youtube.com"}:
        return "потрібна взаємодія зі сторінкою згоди"
    if path.startswith(("/accounts/login", "/login", "/checkpoint")):
        return "потрібен вхід або перевірка доступу"
    plain = unescape(re.sub(r"<[^>]*>", " ", text)).lower()
    # Не реагуємо на слово CAPTCHA у звичайній статті чи назві фотографії.
    strong = ("our systems have detected unusual traffic", "unusual traffic from your computer network",
              "наші системи виявили незвичний трафік", "наши системы обнаружили необычный трафик",
              "wykryły nietypowy ruch", "verify you are human", "verify that you are human",
              "підтвердьте, що ви людина", "potwierdź, że jesteś człowiekiem")
    if any(phrase in plain[:2500] for phrase in strong):
        return "CAPTCHA / перевірка людини"
    if "just a moment" in title.lower() and ("challenge" in text.lower() or "cloudflare" in plain):
        return "перевірка браузера"
    return ""


class AccessPolicy:
    """Список пропущених доменів живе лише протягом одного пошуку."""
    def __init__(self):
        self.blocked: dict[str, str] = {}

    def check(self, url: str) -> None:
        host = hostname(url)
        if host in self.blocked:
            raise SourceBlocked(url, self.blocked[host])

    def block(self, url: str, reason: str):
        self.blocked[hostname(url)] = reason
        raise SourceBlocked(url, reason)

    def check_text(self, url: str, text: str, title: str = "") -> None:
        self.check(url)
        reason = gate_reason(url, text, title)
        if reason:
            self.block(url, reason)

    def check_page(self, page, status: int | None = None) -> None:
        self.check(page.url)
        reason = gate_reason(page.url)
        if reason:
            self.block(page.url, reason)
        if status in {401, 403, 429}:
            self.block(page.url, f"HTTP {status}: доступ обмежений")
        text = page.locator("body").inner_text(timeout=3000)
        self.check_text(page.url, text, page.title())
        for selector in ("form#captcha-form", ".g-recaptcha", ".h-captcha",
                         "iframe[title*='recaptcha' i]", "iframe[title*='hcaptcha' i]",
                         "iframe[src*='challenges.cloudflare.com']"):
            nodes = page.locator(selector)
            if any(nodes.nth(i).is_visible() for i in range(min(nodes.count(), 3))):
                self.block(page.url, "CAPTCHA")

