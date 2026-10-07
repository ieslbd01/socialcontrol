"""Media matching (TDD-04 section 5): explicit > post_id > content_id; carousel numbering."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field


@dataclass
class MatchRequest:
    post_id: str
    content_id: str
    media_files: list[str]  # explicit tokens from the CSV (names or URLs), may be empty
    needs_media: bool


@dataclass
class MatchResult:
    matched: dict[str, list[str]] = field(default_factory=dict)  # post_id -> names / URLs
    errors: dict[str, tuple[str, str]] = field(default_factory=dict)  # post_id -> (code, msg)
    unmatched_files: list[str] = field(default_factory=list)  # W001


def _index(files: Iterable[str]) -> dict[str, list[str]]:
    """stem (lower-case) -> file names sharing that stem."""
    idx: dict[str, list[str]] = {}
    for name in files:
        stem = name.rpartition(".")[0].lower()
        idx.setdefault(stem, []).append(name)
    return idx


def _numbered(idx: Mapping[str, list[str]], base: str) -> list[str]:
    """Carousel files base_1, base_2, ... in numeric order."""
    pat = re.compile(rf"^{re.escape(base.lower())}_(\d+)$")
    found = []
    for stem, names in idx.items():
        m = pat.match(stem)
        if m:
            found.append((int(m.group(1)), names))
    found.sort()
    out: list[str] = []
    for _, names in found:
        out.extend(sorted(names))
    return out


def match_media(requests: Iterable[MatchRequest], available: Iterable[str]) -> MatchResult:
    names = list(available)
    by_lower = {n.lower(): n for n in names}
    idx = _index(names)
    used: set[str] = set()
    result = MatchResult()

    for req in requests:
        chosen: list[str] = []
        if req.media_files:  # 1) explicit mapping
            for token in req.media_files:
                if "://" in token:
                    chosen.append(token)  # URL: fetched and validated later
                    continue
                hit = by_lower.get(token.lower())
                if hit is None:
                    result.errors[req.post_id] = ("E041", f"media file {token!r} not found")
                    break
                chosen.append(hit)
            else:
                pass
            if req.post_id in result.errors:
                continue
        else:
            for key in (req.post_id, req.content_id):  # 2) post_id, 3) content_id
                hits = idx.get(key.lower(), [])
                if len(hits) > 1:
                    result.errors[req.post_id] = (
                        "E042",
                        f"ambiguous media for {key}: {', '.join(sorted(hits))}",
                    )
                    break
                if len(hits) == 1:
                    chosen = [hits[0]]
                    break
                numbered = _numbered(idx, key)  # carousel: <id>_1, <id>_2
                if numbered:
                    chosen = numbered
                    break
            if req.post_id in result.errors:
                continue
        if not chosen and req.needs_media:
            result.errors[req.post_id] = ("E040", "required media missing")
            continue
        if chosen:
            result.matched[req.post_id] = chosen
            used.update(c for c in chosen if "://" not in c)

    result.unmatched_files = sorted(n for n in names if n not in used)
    return result
