"""Синтаксична перевірка зовнішніх посилань без DNS або доступу до мережі."""

import ipaddress
from urllib.parse import urlsplit, urlunsplit


def remote_url(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError("Некоректне зовнішнє посилання.")
    value = value.strip()
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.port not in {None, 80, 443} or any(ord(c) < 32 for c in value)):
        raise ValueError("Дозволені лише публічні HTTP/HTTPS-посилання.")
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Локальна адреса.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Приватна адреса.")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
