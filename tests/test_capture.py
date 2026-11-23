"""Справжній браузерний тест. Запуск окремо: pytest tests/test_capture.py."""

from io import BytesIO
from threading import Event

import pytest
from PIL import Image
from playwright.sync_api import sync_playwright

from lazyref.capture import BrowserCapture
from lazyref.access import SourceBlocked
from lazyref.models import Candidate


def test_crop_and_overlay_rejection():
    buffer = BytesIO()
    Image.new("RGB", (420, 320), "#448844").save(buffer, "PNG")
    url = "https://example.org/target.png"
    with sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(channel="msedge", headless=True)
        except Exception:
            pytest.skip("Microsoft Edge недоступний у цьому середовищі")
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 800})
            page.route(url, lambda route: route.fulfill(body=buffer.getvalue(), content_type="image/png"))
            page.set_content(f'<h1>Interface outside the crop</h1><div class="g-recaptcha" style="display:none"></div><img src="{url}" width="420" height="320">')
            candidate = Candidate("test", "test", image_url=url)
            capture = BrowserCapture(Event())
            result = capture.capture_element(page, candidate)
            with Image.open(BytesIO(result)) as image:
                # Chromium округлює дробові CSS-координати рамки до цілого пікселя.
                assert abs(image.width - 420) <= 1 and abs(image.height - 320) <= 1
                assert image.getpixel((10, 10))[:3] == (68, 136, 68)
            page.evaluate("""() => {
                const overlay=document.createElement('div');
                overlay.style.cssText='position:fixed;inset:0;background:white;z-index:9999';
                document.body.appendChild(overlay);
            }""")
            with pytest.raises(ValueError):
                capture.capture_element(page, candidate)
            page.set_content(f'<h1>Verify you are human</h1><img src="{url}" width="420" height="320">')
            with pytest.raises(SourceBlocked):
                capture.capture_element(page, candidate)
        finally:
            browser.close()
