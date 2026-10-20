"""Desktop popup and sound on the server PC (Windows 10/11, no extra packages).

Uses the built-in Windows PowerShell 5.1 toast API and ``winsound``. On other
systems, or when the server runs as a background service without a desktop,
this silently does nothing.
"""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)

# Windows PowerShell's own app id, so toasts work without registering an app.
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast scenario="reminder"><visual><binding template="ToastGeneric"><text>__TITLE__</text><text>__BODY__</text></binding></visual><audio src="ms-winsoundevent:Notification.Looping.Alarm2" loop="false"/></toast>')
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('__APPID__').Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;&apos;"))  # '' escapes ' inside the PS string


def show(title: str, body: str, critical: bool = False) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONHAND if critical else winsound.MB_ICONEXCLAMATION)
    except Exception:  # pragma: no cover - Windows only
        pass
    script = (_PS.replace("__TITLE__", _xml_escape(title[:120]))
              .replace("__BODY__", _xml_escape(body[:400])).replace("__APPID__", APP_ID))
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except OSError as exc:  # pragma: no cover - Windows only
        log.warning("Desktop notification failed: %s", exc)
        return False
