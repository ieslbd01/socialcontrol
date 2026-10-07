from datetime import UTC, datetime, timedelta

from socialcontrol.domain.enums import PatternMode, SlotState
from socialcontrol.scheduler.slots import Candidate, Slot, allocate, compact, free_slots

NOW = datetime(2026, 9, 30, tzinfo=UTC)
T0 = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)


def make_slots(n: int, step_days: int = 7) -> list[Slot]:
    return [Slot(slot_at=T0 + timedelta(days=step_days * i)) for i in range(n)]


def cands(*specs: tuple[str, str]) -> list[Candidate]:
    return [Candidate(post_id=p, post_type=t, queue_position=i) for i, (p, t) in enumerate(specs)]


def placed(slots: list[Slot]) -> list[str | None]:
    return [s.post_id for s in sorted(slots, key=lambda s: s.slot_at)]


def test_fifo_without_pattern_and_continuation():
    slots = make_slots(10)
    r1 = allocate(
        slots, cands(*[(f"C00{i}", "image") for i in range(1, 6)]), [], 0, PatternMode.RELAXED, NOW
    )
    assert placed(slots)[:6] == ["C001", "C002", "C003", "C004", "C005", None]
    assert r1.unassigned == []
    # second batch continues from the next free slot, nothing re-assigned
    allocate(
        slots, cands(*[(f"C00{i}", "image") for i in range(6, 9)]), [], 0, PatternMode.RELAXED, NOW
    )
    assert placed(slots) == [f"C00{i}" for i in range(1, 9)] + [None, None]


def test_never_fills_past_candidates_or_pulls_forward():
    slots = make_slots(4)
    r = allocate(slots, cands(("A", "image")), [], 0, PatternMode.RELAXED, NOW)
    assert placed(slots) == ["A", None, None, None]
    assert r.unassigned == []


def test_min_lead_leaves_imminent_slots_open():
    slots = [Slot(slot_at=NOW + timedelta(minutes=10)), Slot(slot_at=NOW + timedelta(hours=2))]
    allocate(slots, cands(("A", "image"), ("B", "image")), [], 0, PatternMode.RELAXED, NOW)
    assert placed(slots) == [None, "A"]


def test_pattern_relaxed_falls_back_to_next_candidate():
    pattern = ["A", "B", "C"]
    slots = make_slots(5)
    pool = cands(("p1", "A"), ("p2", "B"), ("p3", "A"), ("p4", "C"), ("p5", "B"))
    r = allocate(slots, pool, pattern, 0, PatternMode.RELAXED, NOW)
    # slot1 A->p1, slot2 B->p2, slot3 C expected but none first... C exists (p4) so p4
    assert placed(slots) == ["p1", "p2", "p4", "p3", "p5"]
    assert r.pointer == 5 % 3


def test_pattern_relaxed_no_expected_type_takes_any():
    slots = make_slots(2)
    pool = cands(("p1", "B"), ("p2", "B"))
    allocate(slots, pool, ["A", "B"], 0, PatternMode.RELAXED, NOW)
    assert placed(slots) == ["p1", "p2"]


def test_pattern_strict_skips_to_next_matching_step():
    slots = make_slots(4)
    pool = cands(("p1", "A"), ("p2", "C"), ("p3", "A"))
    r = allocate(slots, pool, ["A", "B", "C"], 0, PatternMode.STRICT, NOW)
    # A -> p1; B missing -> skip to C -> p2; then A -> p3
    assert placed(slots) == ["p1", "p2", "p3", None]
    assert [s.pattern_index for s in slots[:3]] == [0, 2, 0]
    assert r.pointer == 1


def test_pattern_strict_unfittable_candidates_stay_unassigned():
    slots = make_slots(3)
    r = allocate(slots, cands(("p1", "X")), ["A", "B"], 0, PatternMode.STRICT, NOW)
    assert placed(slots) == [None, None, None]
    assert r.unassigned == ["p1"]


def test_pointer_persists_across_batches():
    pattern = ["A", "B"]
    slots = make_slots(4)
    r1 = allocate(slots, cands(("p1", "A")), pattern, 0, PatternMode.RELAXED, NOW)
    assert r1.pointer == 1
    allocate(slots, cands(("p2", "B"), ("p3", "A")), pattern, r1.pointer, PatternMode.RELAXED, NOW)
    assert placed(slots) == ["p1", "p2", "p3", None]


def test_pinned_post_takes_its_slot_and_is_excluded_from_fifo():
    slots = make_slots(4)
    pinned = Candidate("PIN", "image", queue_position=99, pinned_slot_at=slots[2].slot_at)
    allocate(
        slots, [*cands(("a", "image"), ("b", "image")), pinned], [], 0, PatternMode.RELAXED, NOW
    )
    assert placed(slots) == ["a", "b", "PIN", None]
    assert slots[2].filled_by == "OVERRIDE"


def test_cancel_shift_up_vector9():
    slots = make_slots(6)
    pool = cands(*[(f"p{i}", "image") for i in range(1, 6)])
    allocate(slots, pool, [], 0, PatternMode.RELAXED, NOW)
    assert placed(slots)[:5] == ["p1", "p2", "p3", "p4", "p5"]
    # cancel p3
    free_slots(slots, {"p3"})
    order = {c.post_id: c for c in pool if c.post_id != "p3"}
    compact(slots, order, [], PatternMode.RELAXED, NOW)
    assert placed(slots)[:5] == ["p1", "p2", "p4", "p5", None]


def test_cancel_shift_up_keeps_pattern_alignment():
    pattern = ["A", "B"]
    slots = make_slots(6)
    pool = cands(("a1", "A"), ("b1", "B"), ("a2", "A"), ("b2", "B"), ("a3", "A"))
    allocate(slots, pool, pattern, 0, PatternMode.RELAXED, NOW)
    free_slots(slots, {"b1"})
    order = {c.post_id: c for c in pool if c.post_id != "b1"}
    r = compact(slots, order, pattern, PatternMode.RELAXED, NOW)
    # pattern A,B,A,B is re-applied from the first freed index: b2 moves up to keep
    # the A/B rhythm, a3 fills the last slot because no B remains (RELAXED).
    assert placed(slots)[:5] == ["a1", "b2", "a2", "a3", None]
    assert all(s.state == SlotState.FILLED for s in slots[:4])
    assert r.pointer == 0


def test_free_slots_only_releases_filled():
    slots = make_slots(2)
    allocate(slots, cands(("a", "image")), [], 0, PatternMode.RELAXED, NOW)
    freed = free_slots(slots, {"a", "zzz"})
    assert len(freed) == 1 and slots[0].state == SlotState.OPEN
