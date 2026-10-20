"""Password hashing, secret encryption and CSRF helpers."""
from __future__ import annotations

import hmac
import secrets
from pathlib import Path

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request

MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def password_problem(password: str, username: str = "") -> str | None:
    """Return a human readable reason why a password is too weak, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if username and password.lower() == username.lower():
        return "Password must not be the same as the username."
    if not any(c.isdigit() for c in password) or not any(c.isalpha() for c in password):
        return "Password must contain both letters and numbers."
    return None


class SecretBox:
    """Encrypts device passwords at rest with a key stored in the data folder.

    Keep ``secret.key`` safe: without it stored device passwords cannot be read,
    and anyone holding it together with the database can read them.
    """

    def __init__(self, key_file: Path):
        if key_file.exists():
            key = key_file.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            key_file.write_bytes(key)
            try:
                key_file.chmod(0o600)
            except OSError:  # pragma: no cover - Windows ignores POSIX modes
                pass
        self._fernet = Fernet(key)
        # Separate derived value for signing session cookies.
        self.session_secret = hmac.new(key, b"session-cookie", "sha256").hexdigest()

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return ""


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    """FastAPI dependency: every state-changing form must carry the session token."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    form = await request.form()
    sent = str(form.get("csrf_token", "")) or request.headers.get("x-csrf-token", "")
    expected = request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        raise HTTPException(status_code=400, detail="Invalid or missing form token. Reload the page and try again.")
