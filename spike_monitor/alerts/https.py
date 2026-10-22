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


DNS_HINT = (" The server PC could not look up the address (DNS): it has no internet at the moment, or its DNS "
            "server does not answer. On the server PC, try 'nslookup api.telegram.org' in Command Prompt; if it "
            "fails, set the network adapter's DNS to 1.1.1.1 and 8.8.8.8 (or fix DNS on the router it uses).")
DNS_ERRORS = ("getaddrinfo failed", "Errno 11001", "Errno 11004", "Name or service not known",
              "Temporary failure in name resolution", "nodename nor servname")


def explain(exc: BaseException) -> str:
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return text + HINT
    if any(e in text for e in DNS_ERRORS):
        return text + DNS_HINT
    return text
