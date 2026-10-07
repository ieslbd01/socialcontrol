"""Import service: dry-run, confirm, undo (PRD-03, TDD-04 sections 6-7).

Media bytes are handed over by the caller through a ``StorageBackend``; this module
never talks to Supabase/R2 directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Connection, text

from socialcontrol.domain.workflow import approval_hash
from socialcontrol.imports.csv_importer import (
    Issue,
    Level,
    ParseResult,
    PostRow,
    Registry,
    parse_csv,
)
from socialcontrol.media.matcher import MatchRequest, match_media
from socialcontrol.media.zipsafe import MediaFile, read_zip


class StorageBackend(Protocol):
    def put(self, key: str, data: bytes, mime: str) -> str:
        """Store bytes and return a URL (public where the channel needs it)."""
        ...


@dataclass
class DryRun:
    batch_id: str
    parse: ParseResult
    issues: list[Issue]
    matched: dict[str, list[str]]
    unmatched_files: list[str]
    summary: dict[str, int]


# ---------------------------------------------------------------- registry from DB
def load_registry(conn: Connection) -> Registry:
    platform_codes = {
        r[0]: r[1] for r in conn.execute(text("select key, code from platforms where enabled"))
    }
    accounts = {
        (r[0], r[1]): r[2]
        for r in conn.execute(text("select platform_key, short_name, state from platform_accounts"))
    }
    queues: dict[tuple[str, str], set[str]] = {}
    for pk, acc, name in conn.execute(
        text(
            """select a.platform_key, a.short_name, q.name from queues q
               join platform_accounts a on a.id = q.account_id"""
        )
    ):
        queues.setdefault((pk, acc), set()).add(name)
    caps: dict[tuple[str, str], dict[str, Any]] = {}
    for r in conn.execute(text("select * from platform_capabilities")).mappings():
        caps[(r["platform_key"], r["post_type"])] = {
            "max_caption_chars": r["max_caption_chars"],
            "max_hashtags": r["max_hashtags"],
            "requires_media": r["requires_media"],
            "extra": r["extra"] or {},
        }
    existing = {r[0]: r[1] for r in conn.execute(text("select post_id, status from posts"))}
    return Registry(platform_codes, accounts, queues, caps, existing)


# ---------------------------------------------------------------- dry run
def dry_run(
    conn: Connection,
    csv_bytes: bytes,
    zip_blobs: list[bytes],
    csv_name: str = "content.csv",
) -> tuple[DryRun, list[MediaFile]]:
    registry = load_registry(conn)
    parsed = parse_csv(csv_bytes, registry)
    issues = list(parsed.issues)

    files: list[MediaFile] = []
    for blob in zip_blobs:
        zr = read_zip(blob)
        files.extend(zr.files)
        issues.extend(Issue(0, "E043", Level.ERROR, f"{name}: {why}") for name, why in zr.rejected)

    needs = {pk_type: bool(c.get("requires_media")) for pk_type, c in registry.capabilities.items()}
    requests = [
        MatchRequest(
            r.post_id, r.content_id, r.media_files, needs.get((r.platform_key, r.post_type), False)
        )
        for r in parsed.rows
    ]
    mres = match_media(requests, [f.name for f in files])

    # media errors demote rows out of the importable set
    bad_rows: dict[str, tuple[str, str]] = mres.errors
    good: list[PostRow] = []
    for row in parsed.rows:
        if row.post_id in bad_rows:
            code, msg = bad_rows[row.post_id]
            issues.append(Issue(row.row, code, Level.ERROR, msg))
        else:
            good.append(row)
    parsed.rows = good
    for name in mres.unmatched_files:
        issues.append(Issue(0, "W001", Level.WARNING, f"media file not used by any row: {name}"))
    parsed.issues = issues

    batch_id = conn.execute(
        text(
            """insert into import_batches (csv_name, status, counts, report)
               values (:n, 'DRY_RUN', cast(:c as jsonb), cast(:r as jsonb)) returning id"""
        ),
        {
            "n": csv_name,
            "c": json.dumps(parsed.summary),
            "r": json.dumps(
                [
                    {"row": i.row, "code": i.code, "level": i.level.value, "message": i.message}
                    for i in issues
                ]
            ),
        },
    ).scalar_one()
    return (
        DryRun(str(batch_id), parsed, issues, mres.matched, mres.unmatched_files, parsed.summary),
        files,
    )


# ---------------------------------------------------------------- confirm
def confirm(
    conn: Connection,
    run: DryRun,
    files: list[MediaFile],
    storage: StorageBackend,
    include_warnings: bool = True,
    overwrite: bool = False,
) -> dict[str, int]:
    """Persist rows of a dry run. Idempotent: re-importing identical rows changes nothing."""
    by_name = {f.name: f for f in files}
    row_has_warning = {i.row for i in run.issues if i.level == Level.WARNING and i.row > 0}
    counts = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0}

    conn.execute(
        text("update import_batches set status='CONFIRMING' where id=:b"), {"b": run.batch_id}
    )
    for row in run.parse.rows:
        if row.row in row_has_warning and not include_warnings:
            counts["skipped"] += 1
            continue
        outcome = _upsert_row(conn, row, run, by_name, storage, overwrite)
        counts[outcome] += 1
    conn.execute(
        text("update import_batches set status='CONFIRMED', counts=cast(:c as jsonb) where id=:b"),
        {"b": run.batch_id, "c": json.dumps({**run.summary, **counts})},
    )
    return counts


def _store_media(
    conn: Connection, f: MediaFile, storage: StorageBackend, backend: str = "supabase"
) -> Any:
    existing = conn.execute(text("select id from media where sha256=:h"), {"h": f.sha256}).scalar()
    if existing:
        return existing  # identical bytes are stored once (IMP-10)
    key = f"{f.sha256[:2]}/{f.sha256}.{f.ext}"
    url = storage.put(key, f.data, f.mime)
    return conn.execute(
        text(
            """insert into media (sha256, filename, mime, bytes, backend, storage_key, public_url)
               values (:h, :n, :m, :b, :be, :k, :u) returning id"""
        ),
        {
            "h": f.sha256,
            "n": f.name,
            "m": f.mime,
            "b": len(f.data),
            "be": backend,
            "k": key,
            "u": url,
        },
    ).scalar_one()


def _upsert_row(
    conn: Connection,
    row: PostRow,
    run: DryRun,
    by_name: dict[str, MediaFile],
    storage: StorageBackend,
    overwrite: bool,
) -> str:
    account_id = conn.execute(
        text("select id from platform_accounts where platform_key=:p and short_name=:s"),
        {"p": row.platform_key, "s": row.account},
    ).scalar_one()
    queue_id = None
    if row.queue:
        queue_id = conn.execute(
            text("select id from queues where account_id=:a and name=:n"),
            {"a": account_id, "n": row.queue},
        ).scalar()
    else:
        queue_id = conn.execute(
            text("select id from queues where account_id=:a and is_default"), {"a": account_id}
        ).scalar()

    # content item (first row wins for title / website)
    conn.execute(
        text(
            """insert into content_items (content_id, title, website_url, tags)
               values (:c, :t, :w, :tags) on conflict (content_id) do nothing"""
        ),
        {
            "c": row.content_id,
            "t": row.title or row.content_id,
            "w": row.website_url or None,
            "tags": row.tags,
        },
    )

    # media
    media_ids: list[Any] = []
    for name in run.matched.get(row.post_id, []):
        if name in by_name:
            media_ids.append(_store_media(conn, by_name[name], storage))
    sha = [
        str(conn.execute(text("select sha256 from media where id=:i"), {"i": m}).scalar_one())
        for m in media_ids
    ]

    existing = (
        conn.execute(
            text(
                "select status, caption, title, link_url, hashtags, post_type, language, evergreen, deleted_at "
                "from posts where post_id=:p"
            ),
            {"p": row.post_id},
        )
        .mappings()
        .first()
    )

    fields = {
        "post_type": row.post_type,
        "language": row.language,
        "title": row.title or None,
        "caption": row.caption,
        "link_url": row.link or None,
        "hashtags": row.hashtags,
        "evergreen": row.evergreen,
    }

    if existing is None:
        status = "APPROVED" if row.approved else "DRAFT"
        h = None
        if status == "APPROVED":
            h = approval_hash({**fields, "media_sha256": sha, "account_id": str(account_id)})
        conn.execute(
            text(
                """insert into posts (post_id, content_id, account_id, queue_id, post_type, language,
                       title, caption, link_url, hashtags, evergreen, status, approved_at,
                       approved_hash, source_batch, notes)
                   values (:post_id, :cid, :acc, :q, :post_type, :language, :title, :caption,
                       :link_url, :hashtags, :evergreen, :status, :ap, :h, :b, :notes)"""
            ),
            {
                **fields,
                "post_id": row.post_id,
                "cid": row.content_id,
                "acc": account_id,
                "q": queue_id,
                "status": status,
                "ap": datetime_now(conn) if status == "APPROVED" else None,
                "h": h,
                "b": run.batch_id,
                "notes": row.notes or None,
            },
        )
        _attach(conn, row.post_id, media_ids)
        _audit(conn, row.post_id, "IMPORTED", status)
        return "new"

    status = existing["status"]
    same = all(existing[k] == v for k, v in fields.items() if k != "hashtags") and list(
        existing["hashtags"] or []
    ) == list(row.hashtags)
    cur_media = [
        r[0]
        for r in conn.execute(
            text("""select m.sha256 from post_media pm join media m on m.id=pm.media_id
                    where pm.post_id=:p order by pm.sort"""),
            {"p": row.post_id},
        )
    ]
    if (
        same
        and cur_media == sha
        and existing["deleted_at"] is None
        and status
        not in (
            "CANCELLED",
            "SKIPPED",
            "FAILED_FINAL",
        )
    ):
        return "unchanged"
    if status in ("APPROVED", "SCHEDULED", "QUEUED") and not overwrite:
        return "skipped"
    new_status = (
        "IN_REVIEW"
        if status in ("APPROVED", "SCHEDULED", "QUEUED")
        else ("DRAFT" if status in ("FAILED_FINAL", "CANCELLED", "SKIPPED") else status)
    )
    if status in ("APPROVED", "SCHEDULED", "QUEUED"):
        conn.execute(
            text(
                """update queue_slots set state='OPEN', post_id=null, filled_by=null
                   where post_id=:p and state='FILLED'"""
            ),
            {"p": row.post_id},
        )
    conn.execute(
        text(
            """update posts set post_type=:post_type, language=:language, title=:title,
                   caption=:caption, link_url=:link_url, hashtags=:hashtags, evergreen=:evergreen,
                   status=:ns, scheduled_at=null, approved_hash=null, source_batch=:b, deleted_at=null
               where post_id=:p"""
        ),
        {**fields, "p": row.post_id, "ns": new_status, "b": run.batch_id},
    )
    conn.execute(text("delete from post_media where post_id=:p"), {"p": row.post_id})
    _attach(conn, row.post_id, media_ids)
    _audit(conn, row.post_id, "REIMPORTED", f"{status} -> {new_status}")
    return "updated"


def datetime_now(conn: Connection) -> datetime:
    return conn.execute(text("select now()")).scalar_one()  # type: ignore[no-any-return]


def _attach(conn: Connection, post_id: str, media_ids: list[Any]) -> None:
    for i, mid in enumerate(media_ids):
        conn.execute(
            text(
                """insert into post_media (post_id, media_id, role, sort)
                   values (:p, :m, 'primary', :s) on conflict do nothing"""
            ),
            {"p": post_id, "m": mid, "s": i},
        )


def _audit(conn: Connection, post_id: str, action: str, reason: str) -> None:
    conn.execute(
        text("insert into post_audit (post_id, actor, action, reason) values (:p,'import',:a,:r)"),
        {"p": post_id, "a": action, "r": reason},
    )


# ---------------------------------------------------------------- undo
def undo_batch(conn: Connection, batch_id: str) -> dict[str, int]:
    """Soft-delete the batch's posts that are still unpublished (IMP-12)."""
    rows = conn.execute(
        text(
            """select post_id from posts where source_batch=:b
               and status in ('DRAFT','IN_REVIEW','APPROVED','QUEUED','SCHEDULED')"""
        ),
        {"b": batch_id},
    ).all()
    ids = [r[0] for r in rows]
    for pid in ids:
        conn.execute(
            text(
                """update queue_slots set state='OPEN', post_id=null, filled_by=null
                   where post_id=:p and state='FILLED'"""
            ),
            {"p": pid},
        )
        conn.execute(
            text(
                "update posts set status='CANCELLED', scheduled_at=null, deleted_at=now() where post_id=:p"
            ),
            {"p": pid},
        )
        _audit(conn, pid, "IMPORT_UNDONE", batch_id)
    kept = conn.execute(
        text("select count(*) from posts where source_batch=:b and deleted_at is null"),
        {"b": batch_id},
    ).scalar_one()
    conn.execute(text("update import_batches set status='UNDONE' where id=:b"), {"b": batch_id})
    return {"undone": len(ids), "kept": int(kept)}
