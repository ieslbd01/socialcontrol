"""Idempotent seeding of platforms and capabilities from seeds/platforms.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

SEED_FILE = Path(__file__).resolve().parents[3] / "seeds" / "platforms.json"


def load_seed(path: Path = SEED_FILE) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def seed(engine: Engine, path: Path = SEED_FILE) -> dict[str, int]:
    """Upsert platforms and capabilities; safe to run repeatedly."""
    data = load_seed(path)
    with engine.begin() as conn:
        for p in data["platforms"]:
            conn.execute(
                text(
                    """insert into platforms (key, display_name, code, adapter, default_mode)
                       values (:key, :display_name, :code, :adapter, :default_mode)
                       on conflict (key) do update set display_name = excluded.display_name,
                         code = excluded.code, adapter = excluded.adapter,
                         default_mode = excluded.default_mode"""
                ),
                p,
            )
        for c in data["capabilities"]:
            row = {
                "max_caption_chars": None,
                "max_hashtags": None,
                "requires_media": False,
                "media_types": [],
                "extra": {},
                **c,
            }
            row["extra"] = json.dumps(row["extra"])
            conn.execute(
                text(
                    """insert into platform_capabilities
                         (platform_key, post_type, max_caption_chars, max_hashtags,
                          requires_media, media_types, extra)
                       values (:platform_key, :post_type, :max_caption_chars, :max_hashtags,
                          :requires_media, :media_types, cast(:extra as jsonb))
                       on conflict (platform_key, post_type) do update set
                          max_caption_chars = excluded.max_caption_chars,
                          max_hashtags = excluded.max_hashtags,
                          requires_media = excluded.requires_media,
                          media_types = excluded.media_types, extra = excluded.extra"""
                ),
                row,
            )
    return {"platforms": len(data["platforms"]), "capabilities": len(data["capabilities"])}


if __name__ == "__main__":
    from socialcontrol.database.db import make_engine

    print(seed(make_engine()))
