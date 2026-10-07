# SocialControl

Central control system for planning, bulk-importing, scheduling, publishing, monitoring and reporting social content across multiple channels. Design documents live outside this repository (`../docs`).

**Status:** the whole system is built and tested against mock channels. All six channels (Facebook Page, Instagram, YouTube, LinkedIn Company Page, Google Business Profile, WhatsApp Channel) work in **assisted** mode: at the slot time the owner receives a ready-to-post package and confirms with one tap. Per-channel automatic (API) publishing is switched on later, account by account, once each platform approves API access.

## Run it locally

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"      # Linux/macOS: .venv/bin/python
docker compose up -d                                  # local Postgres on 127.0.0.1:5433
cp .env.example .env                                  # then edit; never commit .env
.venv/Scripts/python -m socialcontrol migrate
.venv/Scripts/python -m socialcontrol seed
.venv/Scripts/python -m socialcontrol demo            # optional sample account + queue
.venv/Scripts/python scripts/run_dev.py               # dashboard on http://127.0.0.1:8000 (dev login is printed)
```

Use `127.0.0.1`, not `localhost`, in database URLs on Windows (IPv6 lookups can stall for minutes).

## Commands

| Command | Purpose |
|---|---|
| `python -m socialcontrol publisher` | one publisher cycle (what the scheduled workflow runs) |
| `python -m socialcontrol report daily\|weekly\|monthly` | build, store and send a report |
| `python -m socialcontrol watchdog` | alert if the publisher stopped, tokens expired or queues are low |
| `python -m socialcontrol backup` | encrypted database dump (needs `SC_BACKUP_KEY`, `pg_dump`) |
| `python -m socialcontrol serve` | dashboard (production-style; requires admin env vars) |
| `python -m socialcontrol.dashboard.auth` | create the admin password hash for `SC_ADMIN_PASSWORD_HASH` |

## Quality gates

```bash
.venv/Scripts/python -m ruff check src tests && .venv/Scripts/python -m mypy
.venv/Scripts/python -m pytest        # integration tests need the local Postgres container
```

**Never commit `.env`, content, media, CSVs, logs or database dumps** — this repository is intended to be public and contains code only.
