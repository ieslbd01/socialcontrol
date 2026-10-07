"""Encrypted database backups (OPS-03). Restore: decrypt, then ``pg_restore`` into a fresh database."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.engine import make_url


class BackupError(RuntimeError):
    pass


def encrypt_bytes(data: bytes, key: str) -> bytes:
    if not key:
        raise BackupError("SC_BACKUP_KEY is not set; refusing to write an unencrypted backup")
    return Fernet(key.encode()).encrypt(data)


def decrypt_bytes(blob: bytes, key: str) -> bytes:
    try:
        return Fernet(key.encode()).decrypt(blob)
    except InvalidToken as exc:
        raise BackupError("wrong key or corrupted backup") from exc


def dump_command(database_url: str) -> list[str]:
    """pg_dump arguments. The password goes through PGPASSWORD, never the command line."""
    u = make_url(database_url)
    cmd = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
    if u.host:
        cmd += ["--host", u.host]
    if u.port:
        cmd += ["--port", str(u.port)]
    if u.username:
        cmd += ["--username", u.username]
    cmd += ["--dbname", u.database or "postgres"]
    return cmd


Runner = Callable[[list[str], dict[str, str]], bytes]


def _run(cmd: list[str], env: dict[str, str]) -> bytes:
    done = subprocess.run(cmd, env=env, capture_output=True, check=False)  # noqa: S603
    if done.returncode != 0:
        raise BackupError(f"pg_dump failed: {done.stderr.decode(errors='replace')[:300]}")
    return done.stdout


def run_backup(
    database_url: str, key: str, out_dir: Path, now: datetime, keep: int = 8, runner: Runner = _run
) -> Path:
    """Dump, encrypt, write ``YYYY-MM-DD.dump.enc`` and prune to the newest ``keep`` files."""
    import os

    u = make_url(database_url)
    env = {**os.environ, **({"PGPASSWORD": u.password} if u.password else {})}
    dump = runner(dump_command(database_url), env)
    if len(dump) < 100:
        raise BackupError("dump is suspiciously small; not writing a backup")
    blob = encrypt_bytes(dump, key)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{now:%Y-%m-%d}.dump.enc"
    target.write_bytes(blob)
    files = sorted(out_dir.glob("*.dump.enc"))
    for old in files[:-keep] if keep > 0 else []:
        old.unlink()
    return target
