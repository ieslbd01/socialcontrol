"""Copy one value from .env.prod to the Windows clipboard WITHOUT printing it.

Used to fill GitHub repository secrets without retyping or pasting them anywhere else.

    python scripts/copy_secret.py --list          # which secrets are set (no values shown)
    python scripts/copy_secret.py DATABASE_URL    # copies the value; paste it into GitHub

Clear the clipboard afterwards (copy any other text).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ENV_FILE = Path(".env.prod")
WANTED = [
    "DATABASE_URL",
    "SC_SIGNING_KEY",
    "SC_MASTER_KEY",
    "SC_BACKUP_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if m:
            values[m.group(1)] = m.group(2).strip()
    return values


def set_clipboard(value: str) -> None:
    """Put ``value`` on the clipboard exactly (PowerShell), without showing or storing it."""
    ps = "$v = [Console]::In.ReadToEnd(); Set-Clipboard -Value $v"
    subprocess.run(  # noqa: S603
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],  # noqa: S607
        input=value.encode("ascii"),
        check=True,
    )


def main(argv: list[str]) -> int:
    if not ENV_FILE.exists():
        print(f"{ENV_FILE} not found. Run this from the socialcontrol folder.")
        return 2
    env = read_env(ENV_FILE)
    if not argv or argv[0] == "--list":
        print("GitHub secret name      status")
        for name in WANTED:
            v = env.get(name, "")
            print(f"{name:<22}  {'SET (' + str(len(v)) + ' characters)' if v else 'MISSING'}")
        return 0
    name = argv[0]
    value = env.get(name, "")
    if not value:
        print(f"{name} is empty or missing in {ENV_FILE}")
        return 1
    set_clipboard(value)
    print(f"Copied {name} ({len(value)} characters) to the clipboard. Paste it into GitHub now.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
