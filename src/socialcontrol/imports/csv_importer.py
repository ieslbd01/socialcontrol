"""CSV parsing and row validation (TDD-04 / PRD-03).

Pure logic: no database access. The caller supplies the registry data
(platforms, accounts, queues, capabilities, existing post statuses) as plain
dicts, so the validator is fast to test and independent of storage.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlparse

MAX_ROWS = 5000
CONTENT_ID_RE = re.compile(r"^C[0-9]{1,6}$")
KNOWN_COLUMNS = (
    "content_id,platform,account,queue,post_type,language,title,caption,link,media_file,"
    "hashtags,cta_type,evergreen,approved,tags,notes,website_url,post_suffix"
).split(",")
REQUIRED_COLUMNS = ("content_id", "platform", "account", "post_type")
LANGUAGES = ("en", "bn", "en+bn")
TRUE = {"yes", "true", "1", "y"}
FALSE = {"no", "false", "0", "n", ""}
IMMUTABLE_STATUSES = {"PUBLISHING", "AWAITING_CONFIRMATION", "PUBLISHED"}
OVERWRITE_WARN_STATUSES = {"APPROVED", "SCHEDULED", "QUEUED"}


class Level(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


@dataclass(frozen=True)
class Issue:
    row: int  # 1-based data row number (0 = file level)
    code: str
    level: Level
    message: str


@dataclass
class PostRow:
    row: int
    post_id: str
    content_id: str
    platform_key: str
    account: str
    queue: str | None
    post_type: str
    language: str
    title: str
    caption: str
    link: str
    media_files: list[str]
    hashtags: list[str]
    cta_type: str
    evergreen: bool
    approved: bool
    tags: list[str]
    notes: str
    website_url: str


@dataclass
class Registry:
    """What the validator needs to know about the system (loaded from the DB by the caller)."""

    platform_codes: dict[str, str]  # key -> code (FB...)
    accounts: dict[tuple[str, str], str]  # (platform_key, short_name) -> state
    queues: dict[tuple[str, str], set[str]]  # (platform_key, account) -> queue names
    capabilities: Mapping[tuple[str, str], Mapping[str, object]]
    existing_status: dict[str, str] = field(default_factory=dict)  # post_id -> status

    def resolve_platform(self, value: str) -> str | None:
        v = value.strip().lower()
        if v in self.platform_codes:
            return v
        for key, code in self.platform_codes.items():
            if code.lower() == v:
                return key
        return None


@dataclass
class ParseResult:
    rows: list[PostRow] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def errors_by_row(self) -> dict[int, list[Issue]]:
        out: dict[int, list[Issue]] = {}
        for i in self.issues:
            if i.level == Level.ERROR:
                out.setdefault(i.row, []).append(i)
        return out

    @property
    def summary(self) -> dict[str, int]:
        bad = set(self.errors_by_row()) - {0}
        warn_rows = {i.row for i in self.issues if i.level == Level.WARNING and i.row not in bad}
        valid = len({r.row for r in self.rows} - bad)
        return {
            "valid": valid - len(warn_rows & {r.row for r in self.rows}),
            "warning": len(warn_rows & {r.row for r in self.rows}),
            "error": len(bad),
        }


def _split_list(value: str, seps: str) -> list[str]:
    parts = re.split(f"[{re.escape(seps)}]", value)
    return [p.strip() for p in parts if p.strip()]


def _bool(value: str) -> bool | None:
    v = value.strip().lower()
    if v in TRUE:
        return True
    if v in FALSE:
        return False
    return None


def _valid_url(value: str) -> bool:
    try:
        p = urlparse(value)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def _has_bangla(text: str) -> bool:
    return any("ঀ" <= ch <= "৿" for ch in text)


def _bangla_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(1 for c in letters if "ঀ" <= c <= "৿") / len(letters) if letters else 0.0


def _hashtags(value: str) -> list[str]:
    tags = []
    for t in re.split(r"[\s,]+", value.strip()):
        if t:
            tags.append(t if t.startswith("#") else f"#{t}")
    return tags


def parse_csv(raw: bytes | str, registry: Registry) -> ParseResult:
    """Parse and validate content.csv. Never raises for bad data; reports issues instead."""
    result = ParseResult()
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw.lstrip("﻿")
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        header = [h.strip() for h in (reader.fieldnames or [])]
    except csv.Error as exc:
        result.issues.append(Issue(0, "E001", Level.ERROR, f"cannot read CSV: {exc}"))
        return result
    if not header:
        result.issues.append(Issue(0, "E001", Level.ERROR, "file is empty or has no header"))
        return result

    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        result.issues.append(
            Issue(0, "E001", Level.ERROR, f"missing required column(s): {', '.join(missing)}")
        )
        return result
    for col in header:
        if col and col not in KNOWN_COLUMNS:
            result.issues.append(Issue(0, "W007", Level.WARNING, f"unknown column ignored: {col}"))

    reader.fieldnames = header
    seen: dict[str, int] = {}
    content_meta: dict[str, tuple[str, str]] = {}

    for n, rec in enumerate(reader, start=1):
        if n > MAX_ROWS:
            result.issues.append(
                Issue(0, "E001", Level.ERROR, f"more than {MAX_ROWS} rows; split the file")
            )
            break
        g = {k: (rec.get(k) or "").strip() for k in KNOWN_COLUMNS}
        if not any(g.values()):
            continue  # blank line
        before = len(result.issues)

        def err(code: str, msg: str, _n: int = n) -> None:
            result.issues.append(Issue(_n, code, Level.ERROR, msg))

        def warn(code: str, msg: str, _n: int = n) -> None:
            result.issues.append(Issue(_n, code, Level.WARNING, msg))

        cid = g["content_id"].upper()
        if not CONTENT_ID_RE.match(cid):
            err("E010", f"invalid content_id {g['content_id']!r} (expected like C001)")

        platform_key = registry.resolve_platform(g["platform"])
        if platform_key is None:
            err("E020", f"unknown platform {g['platform']!r}")

        account_state = None
        if platform_key is not None:
            account_state = registry.accounts.get((platform_key, g["account"]))
            if account_state is None:
                err("E021", f"unknown account {g['account']!r} for {platform_key}")
            elif account_state == "DISABLED":
                err("E023", f"account {g['account']!r} is disabled")

        queue = None
        if g["queue"] and g["queue"] != "-":
            queue = g["queue"]
            if platform_key is not None and queue not in registry.queues.get(
                (platform_key, g["account"]), set()
            ):
                err("E022", f"unknown queue {queue!r} for account {g['account']!r}")
        elif not g["queue"] or g["queue"] == "-":
            warn("W009", "queue unassigned")

        caps = registry.capabilities.get((platform_key, g["post_type"])) if platform_key else None
        if platform_key is not None and caps is None:
            err("E030", f"post_type {g['post_type']!r} not supported on {platform_key}")

        language = g["language"].lower() or "en"
        if language not in LANGUAGES:
            err("E060", f"invalid language {g['language']!r} (en, bn, en+bn)")
        evergreen = _bool(g["evergreen"])
        approved = _bool(g["approved"])
        if evergreen is None:
            err("E060", f"invalid evergreen value {g['evergreen']!r}")
        if approved is None:
            err("E060", f"invalid approved value {g['approved']!r}")

        caption = g["caption"]
        title = g["title"]
        hashtags = _hashtags(g["hashtags"])
        media_files = _split_list(g["media_file"], ";")

        if caps is not None:
            limit = caps.get("max_caption_chars")
            resolved = caption.replace("{link}", g["link"]) if g["link"] else caption
            if isinstance(limit, int) and len(resolved) > limit:
                err("E032", f"caption is {len(resolved)} chars; limit {limit}")
            max_tags = caps.get("max_hashtags")
            if isinstance(max_tags, int) and len(hashtags) > max_tags:
                err("E033", f"{len(hashtags)} hashtags; limit {max_tags}")
            if caps.get("requires_media") is False and media_files:
                warn("W002", "media given but this post type does not need media")
            if not caption and g["post_type"] in ("text", "link", "text_image"):
                err("E031", "caption is required for this post type")
            extra = caps.get("extra")
            max_title = extra.get("max_title_chars") if isinstance(extra, dict) else None
            if isinstance(max_title, int) and len(title) > max_title:
                err("E032", f"title is {len(title)} chars; limit {max_title}")
        if platform_key == "youtube" and not title:
            err("E031", "title is required for YouTube")
        if not hashtags:
            warn("W003", "no hashtags")

        for url_value, label in ((g["link"], "link"), (g["website_url"], "website_url")):
            if url_value and not _valid_url(url_value):
                err("E050", f"invalid {label} URL {url_value!r}")
        for m in media_files:
            if "://" in m and not _valid_url(m):
                err("E050", f"invalid media URL {m!r}")

        # language sanity (warning only)
        body = caption or title
        if body:
            if language == "bn" and not _has_bangla(body):
                warn("W010", "language is bn but the text has no Bangla characters")
            elif language == "en" and _bangla_ratio(body) > 0.5:
                warn("W010", "language is en but most of the text is Bangla")

        # duplicate post_id in file
        post_id = ""
        if CONTENT_ID_RE.match(cid) and platform_key is not None:
            code = registry.platform_codes[platform_key]
            suffix = f"-{g['post_suffix']}" if g["post_suffix"] else ""
            post_id = f"{cid}-{code}{suffix}"
            if post_id in seen:
                err("E011", f"duplicate post {post_id} (also row {seen[post_id]})")
            else:
                seen[post_id] = n
            status = registry.existing_status.get(post_id)
            if status in IMMUTABLE_STATUSES:
                err("E070", f"{post_id} is {status} and cannot be changed")
            elif status in OVERWRITE_WARN_STATUSES:
                warn("W008", f"{post_id} is {status}; overwriting returns it to IN_REVIEW")

        # content-level consistency
        if CONTENT_ID_RE.match(cid):
            meta = (title, g["website_url"])
            first = content_meta.setdefault(cid, meta)
            if first != meta and (title and first[0] and title != first[0]):
                warn("W006", f"{cid} has a different title/website than its first row")

        row_has_error = any(i.level == Level.ERROR for i in result.issues[before:])
        if not row_has_error:
            result.rows.append(
                PostRow(
                    row=n,
                    post_id=post_id,
                    content_id=cid,
                    platform_key=platform_key or "",
                    account=g["account"],
                    queue=queue,
                    post_type=g["post_type"],
                    language=language,
                    title=title,
                    caption=caption,
                    link=g["link"],
                    media_files=media_files,
                    hashtags=hashtags,
                    cta_type=g["cta_type"],
                    evergreen=bool(evergreen),
                    approved=bool(approved),
                    tags=_split_list(g["tags"], ";"),
                    notes=g["notes"],
                    website_url=g["website_url"],
                )
            )
    return result


def errors_csv(raw: bytes | str, issues: Iterable[Issue]) -> str:
    """Original rows that failed, plus error_codes/message columns (IMP-07)."""
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    reader = csv.DictReader(io.StringIO(text, newline=""))
    by_row: dict[int, list[Issue]] = {}
    for i in issues:
        if i.level == Level.ERROR and i.row > 0:
            by_row.setdefault(i.row, []).append(i)
    fields = [*(reader.fieldnames or []), "error_codes", "message"]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for n, rec in enumerate(reader, start=1):
        if n in by_row:
            row = {k: _csv_safe(v) for k, v in rec.items() if k in fields}
            row["error_codes"] = ";".join(i.code for i in by_row[n])
            row["message"] = " | ".join(i.message for i in by_row[n])
            writer.writerow(row)
    return out.getvalue()


def _csv_safe(value: object) -> str:
    """Neutralise spreadsheet formula injection on export (SEC-13)."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s
