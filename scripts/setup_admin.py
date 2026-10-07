"""One-step admin setup: login email, password hash and 2FA secret, written to .env.prod.

    python scripts/setup_admin.py            # writes to .env.prod
    python scripts/setup_admin.py --env-file .env

It shows the 2FA setup key on YOUR screen only, asks you to type the 6-digit code from your
authenticator app to prove it works, and writes the settings to the file only after that
check succeeds. Nothing is printed to a log and nothing is sent anywhere.
"""

from __future__ import annotations

import getpass
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

from socialcontrol.dashboard import auth

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 12


class SetupError(Exception):
    pass


def update_env(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in an env file (replace if present, append otherwise)."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for key, value in values.items():
        line = f"{key}={value}"
        if re.search(rf"^{key}=.*$", text, re.M):
            text = re.sub(rf"^{key}=.*$", lambda _m, line=line: line, text, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")


def group(secret: str) -> str:
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


def run(
    env_path: Path,
    ask_email: Callable[[], str],
    ask_password: Callable[[str], str],
    ask_code: Callable[[], str],
    show: Callable[[str], None] = print,
    secret: str | None = None,
    attempts: int = 5,
    now: Callable[[], float] = time.time,
) -> None:
    email = ask_email().strip()
    if not EMAIL_RE.match(email):
        raise SetupError("That does not look like an email address.")
    password = ask_password("Choose a password (at least 12 characters): ")
    if len(password) < MIN_PASSWORD:
        raise SetupError(f"The password must have at least {MIN_PASSWORD} characters.")
    if ask_password("Type the password again: ") != password:
        raise SetupError("The two passwords are different. Nothing was saved.")

    secret = secret or auth.generate_totp_secret()
    show("\nOpen your authenticator app (Google/Microsoft Authenticator):")
    show("  choose 'Add account' -> 'Enter a setup key' and type this key:\n")
    show(f"      {group(secret)}\n")
    show("  Account name: SocialControl     Type: Time based\n")

    verifier = auth.TotpVerifier(secret)
    for left in range(attempts, 0, -1):
        if verifier.verify(ask_code(), now()):
            break
        show(f"That code is not correct ({left - 1} attempt(s) left). Wait for a fresh code.")
    else:
        raise SetupError("The code never matched. Nothing was saved. Run the script again.")

    update_env(
        env_path,
        {
            "SC_ADMIN_EMAIL": email,
            "SC_ADMIN_PASSWORD_HASH": auth.hash_password(password),
            "SC_TOTP_SECRET": secret,
        },
    )
    show(f"\nDone. Saved to {env_path}. You can now sign in with your email, password and code.")


def main(argv: list[str]) -> int:
    env_path = Path(".env.prod")
    if "--env-file" in argv:
        env_path = Path(argv[argv.index("--env-file") + 1])
    if not env_path.exists():
        print(f"{env_path} not found. Run this from the socialcontrol folder.")
        return 2
    try:
        run(
            env_path,
            ask_email=lambda: input("Your login email (e.g. you@ieslbd.com): "),
            ask_password=lambda prompt: getpass.getpass(prompt),
            ask_code=lambda: input("Type the 6-digit code now shown in the app: "),
        )
    except SetupError as exc:
        print(f"\n{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
