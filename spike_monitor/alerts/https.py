"""HTTPS for outgoing notifications (Telegram, WhatsApp).

Antivirus "HTTPS scanning" and some firewalls replace site certificates with their own.
Browsers trust those because the antivirus adds its certificate to the Windows store, but
Python's own certificate list does not. We therefore check certificates against the
operating system's store (``truststore``), and optionally a CA file set in SPIKE_CA_BUNDLE.
Certificate checking is never turned off: the messages carry bot tokens.
"""
from __future__ import annotations

import functools
import os
import ssl
import urllib.request

HINT = (" Something on this PC or network (often antivirus 'HTTPS scanning' or a firewall) is replacing "
        "secure certificates. Turn off HTTPS scanning for python.exe in the antivirus, or set SPIKE_CA_BUNDLE "
        "to that product's root certificate file.")


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get("SPIKE_CA_BUNDLE", "").strip()
    try:
        import truststore

        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # truststore missing or OS store unavailable
        ctx = ssl.create_default_context()
    if cafile:
        ctx.load_verify_locations(cafile=cafile)
    return ctx


def urlopen(req, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())


def explain(exc: BaseException) -> str:
    text = str(exc)
    return text + HINT if "CERTIFICATE_VERIFY_FAILED" in text else text
