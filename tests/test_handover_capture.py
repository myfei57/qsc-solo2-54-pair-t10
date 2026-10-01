"""作业事件自动归集为当班充装事实。"""

from __future__ import annotations

from hydrorefuel.handover import BUFFER_CHARGE, FILL_TRANSACTION, UNLOAD_RECEIPT


def test_operations_collect_facts_into_open_shift(platform):
    platform.start()
    platform.handover_open_shift("S")
    platform.unload(target_kg=10.0)
    platform.pressurize(charge_kg=12.0)
    platform.fill(vehicle="vehicle01", steps=2)

    draft = platform.handover_draft("S")["facts"]
    by_type = {}
    for fact in draft:
        by_type.setdefault(fact["record_type"], []).append(fact["values"])

    assert by_type[UNLOAD_RECEIPT][0]["delivered_kg"] >= 10.0
    assert by_type[BUFFER_CHARGE][0]["charge_kg"] == 12.0
    assert "pressure_mpa" in by_type[BUFFER_CHARGE][0]
    assert by_type[FILL_TRANSACTION][0]["vehicle"] == "vehicle01"
    assert by_type[FILL_TRANSACTION][0]["mass_kg"] > 0


def test_no_open_shift_does_not_block_operations(platform):
    platform.start()
    # 没有开班时，正常作业不应被交接台账阻塞，只是不产生草稿事实。
    assert platform.fill(vehicle="vehicle01", steps=2)["ok"] is True

    # 开班记一条后封口；封口后的作业不再进入这一班。
    platform.handover_open_shift("S")
    platform.handover_record_fact(FILL_TRANSACTION, {"vehicle": "x", "mass_kg": 1.0})
    platform.handover_seal("S")
    assert platform.unload(target_kg=5.0)["ok"] is True
    draft = platform.handover_draft("S")["facts"]
    assert len(draft) == 1
    assert draft[0]["values"]["vehicle"] == "x"
