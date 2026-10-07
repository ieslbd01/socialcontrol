"""Platform adapter registry (TDD-05 section 6).

Real API adapters (Facebook, Instagram, YouTube, LinkedIn, Google Business Profile) are added to
``AUTO_ADAPTERS`` only after their platform approval exists. Until then every channel uses the
generic assisted adapter, which is also the safe fallback for an AUTO account without an adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from socialcontrol.platforms.adapters.assisted import AssistedAdapter
from socialcontrol.platforms.base import PlatformAdapter

AUTO_ADAPTERS: dict[str, Callable[[dict[str, Any]], PlatformAdapter]] = {}


def adapter_for(settings: dict[str, Any]) -> PlatformAdapter:
    """Build the adapter for an account. ``settings`` carries ``_platform_key`` and the account's JSON."""
    key = str(settings.get("_platform_key", ""))
    factory = AUTO_ADAPTERS.get(key)
    if factory is not None and settings.get("_mode") == "AUTO":
        return factory(settings)
    return AssistedAdapter(key, settings)


def register_auto_adapter(key: str, factory: Callable[[dict[str, Any]], PlatformAdapter]) -> None:
    AUTO_ADAPTERS[key] = factory
