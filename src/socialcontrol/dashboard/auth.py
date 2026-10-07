"""Dashboard authentication helpers (PRD-11): scrypt passwords, lockout, CSRF.

V1 is a single admin whose email and password hash come from the environment.
(A Supabase-Auth backend can replace this later without changing the routes.)
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import secrets
import sys
import time
from collections import defaultdict, deque

N, R, P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    salt_b64 = base64.b64encode(salt).decode()
    hash_b64 = base64.b64encode(digest).decode()
    return f"scrypt${N}${R}${P}${salt_b64}${hash_b64}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


class LoginThrottle:
    """Lock a key (ip + email) after ``max_failures`` failures within ``window`` seconds."""

    def __init__(self, max_failures: int = 5, window: int = 600, lockout: int = 600) -> None:
        self.max_failures, self.window, self.lockout = max_failures, window, lockout
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}

    def is_locked(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        until = self._locked_until.get(key)
        if until and now < until:
            return True
        if until:
            self._locked_until.pop(key, None)
            self._fails.pop(key, None)
        return False

    def failure(self, key: str, now: float | None = None) -> bool:
        """Record a failure; returns True if the key is now locked."""
        now = time.time() if now is None else now
        q = self._fails[key]
        q.append(now)
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max_failures:
            self._locked_until[key] = now + self.lockout
            return True
        return False

    def success(self, key: str) -> None:
        self._fails.pop(key, None)
        self._locked_until.pop(key, None)


# ---------------------------------------------------------------- TOTP (RFC 6238)
TOTP_STEP = 30
TOTP_DIGITS = 6


def generate_totp_secret() -> str:
    """Random 160-bit secret, base32 (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, at: float | None = None, step_offset: int = 0) -> str:
    import struct

    at = time.time() if at is None else at
    counter = int(at // TOTP_STEP) + step_offset
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


class TotpVerifier:
    """Accepts the previous, current and next 30-second code (clock drift) once each."""

    def __init__(self, secret: str) -> None:
        self.secret = secret
        self._used: set[int] = set()

    def verify(self, code: str, at: float | None = None) -> bool:
        code = code.strip().replace(" ", "")
        if not (code.isdigit() and len(code) == TOTP_DIGITS):
            return False
        at = time.time() if at is None else at
        base = int(at // TOTP_STEP)
        for offset in (-1, 0, 1):
            step = base + offset
            if hmac.compare_digest(totp_code(self.secret, at, offset), code):
                if step in self._used:
                    return False  # replay of a code that was already accepted
                self._used = {u for u in self._used if u >= base - 2} | {step}
                return True
        return False


def provisioning_uri(secret: str, account: str, issuer: str = "SocialControl") -> str:
    from urllib.parse import quote

    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}"
        f"&issuer={quote(issuer)}&digits={TOTP_DIGITS}&period={TOTP_STEP}"
    )


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_ok(session_token: str | None, submitted: str | None) -> bool:
    if not session_token or not submitted:
        return False
    return hmac.compare_digest(session_token, submitted)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "totp":
    secret = generate_totp_secret()
    account = sys.argv[2] if len(sys.argv) > 2 else "admin"
    print("Add this to the environment:  SC_TOTP_SECRET=" + secret)
    print("In your authenticator app choose 'Enter a setup key' and type the secret above,")
    print("or open this link on the phone that has the app:")
    print(provisioning_uri(secret, account))
    raise SystemExit(0)

if __name__ == "__main__":
    pw = getpass.getpass("New admin password (min 12 chars): ")
    if len(pw) < 12:
        raise SystemExit("password too short")
    if pw != getpass.getpass("Repeat: "):
        raise SystemExit("passwords differ")
    print("SC_ADMIN_PASSWORD_HASH=" + hash_password(pw))
