"""班次交接：签署冻结、追加更正、重开一版、幂等与三方对账。"""

from __future__ import annotations

import json

from hydrorefuel.app import build_platform
from hydrorefuel.clock import ManualClock
from hydrorefuel.config import default_config
from hydrorefuel.errors import InterlockError
from hydrorefuel.shift.model import content_fingerprint, verify_frozen


def _frozen_shift(
    platform,
    signers=("班长甲", "交接人乙"),
    manual_facts=None,
    station="station1",
) -> dict:
    """开一个班、跑一次卸氢+增压+加注并签署冻结，返回冻结结果。"""

    from tests.support import load_station

    load_station(platform)  # 先建立瓶组基线（这一轮发生在开班之前）
    platform.shift_open(station)
    platform.unload(target_kg=10.0)
    platform.fill(steps=2)
    return platform.shift_freeze(list(signers), facts=list(manual_facts or ()))


def _signing_key(platform) -> bytes:
    return platform.store.read("shift_signing_key")["key"].encode("utf-8")


def _fact(version, record_type):
    return next(fact for fact in version["facts"] if fact["record_type"] == record_type)


def test_freeze_produces_signed_immutable_version(platform):
    result = _frozen_shift(platform)
    version = result["version"]
    assert version["freeze_point"] == "on_sign"
    assert version["seq"] == 1
    assert [item["signer"] for item in version["signatures"]] == ["班长甲", "交接人乙"]

    key = _signing_key(platform)
    assert verify_frozen(version, key)["ok"]

    # 冻完草稿清空；没有草稿时再冻结必须被拒绝。
    assert platform.shift_active_draft() is None
    try:
        platform.shift_freeze(["班长甲"])
        raise AssertionError("空冻结应被拒绝")
    except InterlockError:
        pass


def test_handover_only_serves_frozen_never_draft(platform):
    from tests.support import load_station

    load_station(platform)
    platform.shift_open()
    platform.unload(target_kg=10.0)
    platform.fill(steps=2)
    try:
        platform.shift_handover()
        raise AssertionError("草稿不应对外提供")
    except InterlockError as error:
        assert "草稿" in error.message

    platform.shift_freeze(["班长甲"])
    view = platform.shift_handover()
    assert {fact["record_type"] for fact in view["facts"]} == {"unload", "fill"}
    assert _fact(view, "fill")["data"]["filled_kg"] > 0


def test_amend_appends_without_mutating_history(platform):
    frozen = _frozen_shift(
        platform, manual_facts=[{"record_type": "meter_reading", "data": {"totalizer_kg": 100.0}}]
    )["version"]

    # 设备计量事实不允许走追加更正。
    fill = _fact(frozen, "fill")
    try:
        platform.shift_correct(fill["fact_id"], {"filled_kg": 1.0}, "填错", "班长甲", "k-1")
        raise AssertionError("fill 应拒绝 amend")
    except InterlockError as error:
        assert "reopen" in error.message

    # 人工抄表类记录追加更正：历史原件不动，有效视图叠加并标明谁改的。
    meter = _fact(frozen, "meter_reading")
    outcome = platform.shift_correct(
        meter["fact_id"], {"totalizer_kg": 102.5}, "抄表笔误", "复核人丙", "corr-1"
    )
    assert outcome["deduplicated"] is False

    stored = platform.shift_version(frozen["version_id"])["version"]
    assert _fact(stored, "meter_reading")["data"]["totalizer_kg"] == 100.0

    view = platform.shift_handover()
    effective_meter = _fact(view, "meter_reading")
    assert effective_meter["data"]["totalizer_kg"] == 102.5
    change = effective_meter["changes"][0]
    assert (change["old"], change["new"], change["author"], change["reason"]) == (
        100.0,
        102.5,
        "复核人丙",
        "抄表笔误",
    )
    assert len(view["applied_corrections"]) == 1


def test_correction_is_idempotent(platform):
    version = _frozen_shift(
        platform, manual_facts=[{"record_type": "note", "data": {"text": "ok"}}]
    )["version"]
    note = _fact(version, "note")
    first = platform.shift_correct(note["fact_id"], {"text": "fixed"}, "理由", "作者", "idem-9")
    second = platform.shift_correct(note["fact_id"], {"text": "fixed"}, "理由", "作者", "idem-9")
    assert first["correction"]["correction_id"] == second["correction"]["correction_id"]
    assert second["deduplicated"] is True

    # 同一幂等键换了内容必须拒绝。
    try:
        platform.shift_correct(note["fact_id"], {"text": "other"}, "理由", "作者", "idem-9")
        raise AssertionError("幂等键复用应被拒绝")
    except InterlockError:
        pass

    # 无幂等键的另一条更正可追加；流水总计仍是两条。
    platform.shift_correct(note["fact_id"], {"text": "again"}, "另一条", "作者")
    assert len(platform.shift_corrections()["corrections"]) == 2


def test_reopen_creates_child_version_and_keeps_history(platform):
    v1 = _frozen_shift(platform)["version"]
    fill = _fact(v1, "fill")
    original = fill["data"]["filled_kg"]

    opened = platform.shift_reopen(
        fill["fact_id"], {"filled_kg": original + 2.0}, "质量流量计读数偏差", "班长甲", "ro-1"
    )
    assert opened["deduplicated"] is False
    assert opened["draft"]["seq"] == 2
    assert opened["draft"]["parent_version_id"] == v1["version_id"]

    # 重开草稿期间不能开班。
    try:
        platform.shift_open()
        raise AssertionError("活动重开草稿期间开班应被拒绝")
    except InterlockError:
        pass

    # 重开请求重复提交：幂等返回同一草稿，不产生第二份草稿。
    again = platform.shift_reopen(
        fill["fact_id"], {"filled_kg": original + 2.0}, "质量流量计读数偏差", "班长甲", "ro-1"
    )
    assert again["deduplicated"] is True

    refrozen = platform.shift_freeze(["班长甲", "站长丁"])["version"]
    assert refrozen["seq"] == 2
    assert refrozen["parent_version_id"] == v1["version_id"]

    versions = platform.shift_versions(v1["shift_id"])["versions"]
    assert [item["seq"] for item in versions] == [1, 2]

    view = platform.shift_handover()
    assert view["header"]["version_id"] == refrozen["version_id"]
    change = _fact(view, "fill")["changes"][0]
    assert change["kind"] == "reopen"
    assert (change["old"], change["new"]) == (original, original + 2.0)

    corrections = platform.shift_corrections()["corrections"]
    assert len(corrections) == 1
    assert corrections[0]["kind"] == "reopen"
    assert corrections[0]["version_id"] == v1["version_id"]
    assert corrections[0]["new_version_id"] == refrozen["version_id"]

    # 历史版本仍是旧值，并标记被哪个版本接替。
    old_effective = platform.shift_effective(v1["version_id"])
    assert old_effective["superseded_by_version_id"] == refrozen["version_id"]
    assert _fact(old_effective, "fill")["data"]["filled_kg"] == original


def test_reopen_wrong_policy_is_rejected(platform):
    version = _frozen_shift(
        platform, manual_facts=[{"record_type": "note", "data": {"text": "a"}}]
    )["version"]
    note = _fact(version, "note")
    try:
        platform.shift_reopen(note["fact_id"], {"text": "b"}, "理由", "作者", "ro-note")
        raise AssertionError("note 不应重开")
    except InterlockError as error:
        assert "amend" in error.message


def test_three_way_reconcile_passes_and_detects_tampering(platform):
    v1 = _frozen_shift(
        platform, manual_facts=[{"record_type": "meter_reading", "data": {"totalizer_kg": 50.0}}]
    )["version"]
    meter = _fact(v1, "meter_reading")
    platform.shift_correct(meter["fact_id"], {"totalizer_kg": 51.0}, "笔误", "复核人", "c-1")
    fill = _fact(v1, "fill")
    platform.shift_reopen(fill["fact_id"], {"filled_kg": 9.5}, "偏差", "班长甲", "r-1")
    platform.shift_freeze(["班长甲"])

    report = platform.shift_reconcile()
    assert report["ok"] is True, report["failures"]
    assert report["frozen_versions"] == 2
    assert report["corrections"] == 2

    # 篡改磁盘上的冻结原件：指纹对不上，对账必须报失败。
    path = platform.store.path_of(v1["version_id"])
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    raw["facts"][0]["data"]["delivered_kg"] = 999.0
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle)

    tampered = platform.shift_reconcile()
    assert tampered["ok"] is False
    failed_names = {(item["scope"], item["name"]) for item in tampered["checks"] if not item["ok"]}
    # 磁盘原件被改而内存仍是签署件：磁盘一致性检查必须失败。
    assert ("frozen", "disk_copy_matches") in failed_names
    assert content_fingerprint(raw) != v1["fingerprint"]


def test_reconcile_detects_missing_audit(platform):
    version = _frozen_shift(platform)["version"]
    # 显式传入空审计列表：冻结事件"缺失"，三方核对必须报错。
    report = platform.shifts.reconcile([])
    assert report["ok"] is False
    assert any(
        item["scope"] == "audit" and item["name"] == "frozen_event_present" and not item["ok"]
        for item in report["checks"]
    )
    # 正常传入落盘审计则通过。
    assert platform.shift_reconcile()["ok"] is True
    actions = {entry["action"] for entry in platform.audit.replay_from_journal()}
    assert {"shift_opened", "shift_frozen", "shift_fact_recorded"} <= actions
    assert version["version_id"]


def test_freeze_and_correction_events_hit_audit(platform):
    _frozen_shift(
        platform, manual_facts=[{"record_type": "note", "data": {"text": "a"}}]
    )
    versions = platform.shift_versions()["versions"]
    version_id = versions[-1]["version_id"]
    note = _fact(platform.shift_version(version_id)["version"], "note")
    platform.shift_correct(note["fact_id"], {"text": "b"}, "理由", "作者", "c-evt")
    entries = platform.audit.entries(limit=200)
    assert any(
        item["action"] == "shift_frozen" and item["payload"]["freeze_point"] == "on_sign"
        for item in entries
    )
    assert any(
        item["action"] == "shift_corrected"
        and item["payload"]["kind"] == "amend"
        and item["payload"]["author"] == "作者"
        for item in entries
    )


def test_restart_rebuilds_state_from_disk(tmp_path):
    from tests.support import load_station

    root = str(tmp_path / "state")
    clock = ManualClock()
    first = build_platform(default_config(), clock=clock, storage_root=root)
    load_station(first)
    first.shift_open()
    first.unload(target_kg=10.0)
    first.fill(steps=2)
    frozen = first.shift_freeze(["班长甲"])["version"]
    fill = _fact(frozen, "fill")
    first.shift_reopen(fill["fact_id"], {"filled_kg": 7.0}, "偏差", "班长", "rr-1")
    first.shift_freeze(["班长甲"])

    second = build_platform(default_config(), clock=clock, storage_root=root)
    summary = second.shift_summary()
    assert summary["frozen_versions"] == 2
    assert summary["corrections"] == 1
    assert second.shift_handover()["header"]["seq"] == 2
    # 密钥随存储保留，重启后签名仍可验真，对账通过。
    assert second.shift_reconcile()["ok"] is True


def test_console_routes_for_shift(platform):
    import urllib.error
    import urllib.request

    from hydrorefuel.console.server import ConsoleServer

    def post(path, payload):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{base}{path}", data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def get(path):
        with urllib.request.urlopen(f"{base}{path}", timeout=5) as response:
            return response.status, json.loads(response.read())

    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        assert post("/api/shift/open", {})[0] == 200
        assert post("/api/unload", {"target_kg": 10.0})[1]["ok"] is True
        assert post("/api/pressurize", {"charge_kg": 12.0})[1]["ok"] is True
        assert post("/api/fill", {"steps": 2})[1]["ok"] is True
        status, body = post("/api/shift/freeze", {"signers": ["班长甲", "交接人乙"]})
        assert status == 200
        version_id = body["version"]["version_id"]

        assert get("/api/shift/handover")[1]["header"]["version_id"] == version_id
        assert get("/api/shift/reconcile")[1]["ok"] is True
        assert len(get("/api/shift/versions")[1]["versions"]) == 1
        assert get("/api/shift/corrections")[1]["corrections"] == []
        assert get("/api/shift/summary")[1]["frozen_versions"] == 1
        # 历史原件可读。
        assert get(f"/api/shift/version/{version_id}")[1]["version"]["version_id"] == version_id
    finally:
        server.stop()
