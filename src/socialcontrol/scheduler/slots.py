"""Slot allocator (TDD-03 sections 4-5). Pure logic over in-memory objects.

The database layer loads slots/posts, calls these functions inside a transaction
(with an advisory lock per queue) and persists the result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from socialcontrol.domain.enums import PatternMode, SlotState


@dataclass
class Slot:
    slot_at: datetime
    state: SlotState = SlotState.OPEN
    post_id: str | None = None
    pattern_index: int | None = None
    filled_by: str | None = None  # FIFO | OVERRIDE | EVERGREEN


@dataclass(frozen=True)
class Candidate:
    """An APPROVED, not yet slotted post waiting in a queue."""

    post_id: str
    post_type: str
    queue_position: int = 0
    pinned_slot_at: datetime | None = None


@dataclass
class AllocationResult:
    assignments: list[tuple[Slot, str]] = field(default_factory=list)
    pointer: int = 0
    unassigned: list[str] = field(default_factory=list)


def _fill(slot: Slot, post_id: str, index: int | None, by: str) -> None:
    slot.state = SlotState.FILLED
    slot.post_id = post_id
    slot.pattern_index = index
    slot.filled_by = by


def allocate(
    slots: Sequence[Slot],
    candidates: Sequence[Candidate],
    pattern: Sequence[str],
    pointer: int,
    mode: PatternMode,
    now: datetime,
    min_lead: timedelta = timedelta(minutes=30),
) -> AllocationResult:
    """Assign candidates to OPEN future slots in FIFO order.

    - Slots earlier than ``now + min_lead`` are left untouched.
    - Pinned candidates take their pinned slot first and are excluded from FIFO.
    - With a pattern, each slot expects ``pattern[pointer % n]``.
      RELAXED falls back to the next candidate of any type; STRICT skips ahead to
      the next pattern step that has a matching candidate.
    - Allocation stops when candidates run out; later content is never pulled
      earlier to fill gaps (that is the publisher's evergreen/EMPTY handling).
    """
    result = AllocationResult(pointer=pointer)
    open_slots = sorted(
        (s for s in slots if s.state == SlotState.OPEN and s.slot_at >= now + min_lead),
        key=lambda s: s.slot_at,
    )
    pool = sorted(candidates, key=lambda c: (c.queue_position, c.post_id))

    # 1) pinned posts
    remaining: list[Candidate] = []
    by_time = {s.slot_at: s for s in open_slots}
    for cand in pool:
        slot = by_time.get(cand.pinned_slot_at) if cand.pinned_slot_at else None
        if slot is not None and slot.state == SlotState.OPEN:
            _fill(slot, cand.post_id, slot.pattern_index, "OVERRIDE")
            result.assignments.append((slot, cand.post_id))
        else:
            remaining.append(cand)
    pool = remaining  # pinned posts whose slot was unavailable fall back to FIFO

    # 2) FIFO with pattern
    n = len(pattern)
    ptr = pointer
    for slot in open_slots:
        if slot.state != SlotState.OPEN:
            continue
        if not pool:
            break
        if n == 0:
            chosen = pool.pop(0)
            _fill(slot, chosen.post_id, None, "FIFO")
            result.assignments.append((slot, chosen.post_id))
            continue

        chosen_idx = None
        step_used = 0
        for step in range(n if mode == PatternMode.STRICT else 1):
            expected = pattern[(ptr + step) % n]
            hit = next((i for i, c in enumerate(pool) if c.post_type == expected), None)
            if hit is not None:
                chosen_idx, step_used = hit, step
                break
        if chosen_idx is None:
            if mode == PatternMode.STRICT:
                break  # nothing in the pool fits any pattern step
            chosen_idx, step_used = 0, 0  # RELAXED: next candidate of any type

        chosen = pool.pop(chosen_idx)
        index = (ptr + step_used) % n
        _fill(slot, chosen.post_id, index, "FIFO")
        result.assignments.append((slot, chosen.post_id))
        ptr = ptr + step_used + 1

    result.pointer = ptr % n if n else pointer
    result.unassigned = [c.post_id for c in pool]
    return result


def free_slots(slots: Sequence[Slot], post_ids: set[str]) -> list[Slot]:
    """Release the slots held by the given posts (cancel / skip / edit)."""
    freed = []
    for slot in slots:
        if slot.post_id in post_ids and slot.state == SlotState.FILLED:
            slot.state = SlotState.OPEN
            slot.post_id = None
            slot.filled_by = None
            freed.append(slot)
    return freed


def compact(
    slots: Sequence[Slot],
    queue_order: dict[str, Candidate],
    pattern: Sequence[str],
    mode: PatternMode,
    now: datetime,
    min_lead: timedelta = timedelta(minutes=30),
) -> AllocationResult:
    """Shift-up after a cancellation (keep_holes = false).

    Frees every future, non-pinned FILLED slot, then re-allocates the same posts
    in their original order so no hole remains. The pattern pointer restarts from
    the pattern index of the first freed slot.
    """
    horizon = now + min_lead
    movable = sorted(
        (
            s
            for s in slots
            if s.state == SlotState.FILLED
            and s.slot_at >= horizon
            and s.filled_by == "FIFO"
            and s.post_id in queue_order
        ),
        key=lambda s: s.slot_at,
    )
    if not movable:
        return AllocationResult()
    start_index = movable[0].pattern_index or 0
    ids = [s.post_id for s in movable if s.post_id]
    cands = [queue_order[i] for i in ids]
    free_slots(slots, set(ids))
    return allocate(slots, cands, pattern, start_index, mode, now, min_lead)
