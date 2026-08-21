"""The generator has to produce the defects the data quality project claims to catch.

If these tests fail, the companion project is validating nothing.
"""

from __future__ import annotations

from tradepnl.generate import DefectProfile, build_instruments, generate_trades


def test_generator_is_deterministic_for_a_seed() -> None:
    instruments = build_instruments()
    first = generate_trades(instruments, count=500, seed=7)
    second = generate_trades(instruments, count=500, seed=7)
    assert first == second


def test_a_different_seed_produces_different_data() -> None:
    instruments = build_instruments()
    assert generate_trades(instruments, count=500, seed=7) != generate_trades(
        instruments, count=500, seed=8
    )


def test_duplicates_are_appended_so_the_row_count_exceeds_the_request() -> None:
    profile = DefectProfile()
    rows = generate_trades(build_instruments(), count=2_000, defects=profile)
    assert len(rows) == 2_000 + profile.duplicate_trade_ids

    ids = [row.trade_id for row in rows]
    assert len(ids) - len(set(ids)) == profile.duplicate_trade_ids


def test_every_defect_category_is_present() -> None:
    profile = DefectProfile()
    instruments = build_instruments()
    rows = generate_trades(instruments, count=3_000, defects=profile)
    known_ids = {inst.instrument_id for inst in instruments}

    assert sum(1 for r in rows if r.price == "") == profile.null_prices
    assert sum(1 for r in rows if r.executed_at == "") == profile.null_timestamps
    assert sum(1 for r in rows if r.side not in {"BUY", "SELL"}) == profile.bad_sides
    assert sum(1 for r in rows if int(r.quantity) <= 0) == profile.non_positive_quantities
    assert (
        sum(1 for r in rows if int(r.instrument_id) not in known_ids)
        == profile.unknown_instruments
    )


def test_defects_can_be_switched_off() -> None:
    """A clean feed is occasionally useful, for instance when benchmarking the load."""
    profile = DefectProfile(0, 0, 0, 0, 0, 0)
    rows = generate_trades(build_instruments(), count=1_000, defects=profile)

    assert len(rows) == 1_000
    assert len({r.trade_id for r in rows}) == 1_000
    assert all(r.price and r.executed_at for r in rows)


def test_instrument_dimension_is_internally_consistent() -> None:
    instruments = build_instruments()
    assert len({i.instrument_id for i in instruments}) == len(instruments)
    assert len({i.symbol for i in instruments}) == len(instruments)
    assert all(len(i.currency) == 3 for i in instruments)
