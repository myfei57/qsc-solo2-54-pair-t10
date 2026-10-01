"""班次交接：封口、签署冻结、更正与一致性核对。"""

from __future__ import annotations

import json

import pytest

from hydrorefuel.errors import HandoverError
from hydrorefuel.handover import (
    AMEND,
    FILL_TRANSACTION,
    FREEZE_POINT,
    REOPEN,
    SHIFT_ATTESTATION,
    HandoverLedger,
)
from hydrorefuel.handover.policy import correction_mode, is_correctable
from hydrorefuel.handover.signing import digest_content, sign_content, verify_signature

SECRET = "unit-secret"


@pytest.fixture()
def ledger(tmp_path, clock) -> HandoverLedger:
    return HandoverLedger(str(tmp_path / "state"), SECRET, clock)


def _frozen_shift(ledger: HandoverLedger, signer: str = "张三") -> dict:
    """开班 → 记一条加注 → 封口 → 签署，返回当前冻结版。"""

    view = ledger.open_shift()
    ledger.record_fact(FILL_TRANSACTION, {"vehicle": "v01", "mass_kg": 10.0})
    ledger.seal_shift()
    ledger.sign_shift(signer)
    return ledger.current_version(view["shift_id"])


# --------------------------------------------------------------- 口径与策略


def test_freeze_point_is_fixed_at_signing():
    assert FREEZE_POINT == "on_signing"
    assert correction_mode(FILL_TRANSACTION) == AMEND
    assert correction_mode(SHIFT_ATTESTATION) == REOPEN
    assert is_correctable(FILL_TRANSACTION, "mass_kg")
    assert not is_correctable(FILL_TRANSACTION, "vehicle")


def test_signature_covers_content():
    digest = digest_content({"a": 1, "b": 2})
    signature = sign_content("张三", digest, SECRET)
    assert verify_signature("张三", digest, SECRET, signature)
    assert not verify_signature("李四", digest, SECRET, signature)
    assert not verify_signature("张三", digest_content({"a": 1, "b": 3}), SECRET, signature)


# --------------------------------------------------------------- 状态机


def test_freeze_happens_on_signing_not_on_seal(ledger):
    view = ledger.open_shift()
    ledger.record_fact(FILL_TRANSACTION, {"vehicle": "v01", "mass_kg": 10.0})
    sealed = ledger.seal_shift()
    # 班次末只封口：此时还没有冻结版，对外不能取数。
    assert sealed["status"] == "sealed"
    with pytest.raises(HandoverError):
        ledger.current_version()
    version = ledger.sign_shift("张三")
    assert version["version"] == "v1"
    assert version["freeze_point"] == "on_signing"
    assert version["signer"] == "张三"
    assert version["immutable"] is True
    assert version["records"][0]["values"]["mass_kg"] == 10.0


def test_open_shift_cannot_record_facts_after_seal(ledger):
    ledger.open_shift()
    ledger.record_fact(FILL_TRANSACTION, {"mass_kg": 1.0})
    ledger.seal_shift()
    with pytest.raises(HandoverError):
        ledger.record_fact(FILL_TRANSACTION, {"mass_kg": 2.0})


def test_cannot_sign_open_shift_and_cannot_sign_twice(ledger):
    ledger.open_shift()
    ledger.record_fact(FILL_TRANSACTION, {"mass_kg": 1.0})
    with pytest.raises(HandoverError):
        ledger.sign_shift("张三")  # 未封口不能签署
    ledger.seal_shift()
    ledger.sign_shift("张三")
    with pytest.raises(HandoverError):
        ledger.sign_shift("张三")  # 已冻结不能重签


def test_empty_shift_cannot_be_sealed(ledger):
    ledger.open_shift()
    with pytest.raises(HandoverError):
        ledger.seal_shift()


def test_signer_must_be_named(ledger):
    ledger.open_shift()
    ledger.record_fact(FILL_TRANSACTION, {"mass_kg": 1.0})
    ledger.seal_shift()
    with pytest.raises(HandoverError):
        ledger.sign_shift("")


def test_only_one_active_shift(ledger):
    ledger.open_shift("A")
    with pytest.raises(HandoverError):
        ledger.open_shift("B")


# --------------------------------------------------------------- 追加更正


def test_amend_correction_appends_and_marks_change(ledger):
    frozen = _frozen_shift(ledger)
    fact_id = frozen["records"][0]["fact_id"]
    result = ledger.submit_correction(
        fact_id=fact_id,
        field="mass_kg",
        new_value=12.5,
        reason="流量计复核少计",
        author="李四",
        idem_key="idem-1",
    )
    assert result["idempotent"] is False
    assert result["mode"] == AMEND
    assert result["old_value"] == 10.0
    current = ledger.current_version(frozen["shift_id"])
    record = current["records"][0]
    # 有效值改了，但版本号没变、仍是 v1，且改痕清楚可见。
    assert current["version"] == "v1"
    assert current["amended"] is True
    assert record["values"]["mass_kg"] == 12.5
    change = record["changes"][0]
    assert change["old_value"] == 10.0
    assert change["new_value"] == 12.5
    assert change["author"] == "李四"
    assert change["reason"] == "流量计复核少计"


def test_frozen_version_itself_never_changes(ledger):
    frozen = _frozen_shift(ledger)
    fact_id = frozen["records"][0]["fact_id"]
    ledger.submit_correction(
        fact_id=fact_id, field="mass_kg", new_value=12.5,
        reason="x", author="李四", idem_key="idem-1",
    )
    # 直接取历史冻结版 v1：拿到的还是冻那一刻的 10.0。
    v1 = ledger.get_version(frozen["shift_id"], "v1")
    assert v1["records"][0]["values"]["mass_kg"] == 10.0
    assert v1["amended"] is False
    assert v1["content_digest"] == frozen["content_digest"]


def test_amend_cannot_change_attribution_field(ledger):
    frozen = _frozen_shift(ledger)
    fact_id = frozen["records"][0]["fact_id"]
    with pytest.raises(HandoverError):
        ledger.submit_correction(
            fact_id=fact_id, field="vehicle", new_value="v02",
            reason="x", author="李四", idem_key="idem-1",
        )


def test_correction_requires_reason_author_and_idem_key(ledger):
    frozen = _frozen_shift(ledger)
    fact_id = frozen["records"][0]["fact_id"]
    common = dict(fact_id=fact_id, field="mass_kg", new_value=11.0, reason="r", author="a")
    with pytest.raises(HandoverError):
        ledger.submit_correction(**common, idem_key="")
    with pytest.raises(HandoverError):
        ledger.submit_correction(**{**common, "reason": ""}, idem_key="k")
    with pytest.raises(HandoverError):
        ledger.submit_correction(**{**common, "author": ""}, idem_key="k")


def test_identical_value_is_not_a_correction(ledger):
    frozen = _frozen_shift(ledger)
    fact_id = frozen["records"][0]["fact_id"]
    with pytest.raises(HandoverError):
        ledger.submit_correction(
            fact_id=fact_id, field="mass_kg", new_value=10.0,
            reason="x", author="李四", idem_key="idem-1",
        )


# --------------------------------------------------------------- 幂等


def test_duplicate_correction_is_idempotent(ledger):
    frozen = _frozen_shift(ledger)
    fact_id = frozen["records"][0]["fact_id"]
    kwargs = dict(
        fact_id=fact_id, field="mass_kg", new_value=12.5,
        reason="流量计复核", author="李四", idem_key="idem-1",
    )
    first = ledger.submit_correction(**kwargs)
    second = ledger.submit_correction(**kwargs)  # 网络重试
    third = ledger.submit_correction(**kwargs)
    assert second["idempotent"] is True
    assert third["correction_id"] == first["correction_id"]
    corrections = [
        item
        for item in ledger._corrections_journal.read_all()
        if item["kind"] == "correction"
    ]
    assert len(corrections) == 1  # 只落了一条账
    assert ledger.current_version(frozen["shift_id"])["records"][0]["values"]["mass_kg"] == 12.5


def test_same_idem_key_different_content_conflicts(ledger):
    frozen = _frozen_shift(ledger)
    fact_id = frozen["records"][0]["fact_id"]
    ledger.submit_correction(
        fact_id=fact_id, field="mass_kg", new_value=12.5,
        reason="r", author="李四", idem_key="idem-1",
    )
    with pytest.raises(HandoverError):
        ledger.submit_correction(
            fact_id=fact_id, field="mass_kg", new_value=13.0,  # 不同新值
            reason="r", author="李四", idem_key="idem-1",
        )


# --------------------------------------------------------------- 重开一版


def test_attestation_error_reopens_and_signs_v2(ledger):
    ledger.open_shift("S2")
    ledger.record_fact(SHIFT_ATTESTATION, {"result": "正常", "remark": ""})
    ledger.record_fact(FILL_TRANSACTION, {"vehicle": "v02", "mass_kg": 5.0})
    ledger.seal_shift("S2")
    ledger.sign_shift("王五", "S2")
    s2 = ledger.current_version("S2")
    att = next(r for r in s2["records"] if r["record_type"] == SHIFT_ATTESTATION)
    assert correction_mode(SHIFT_ATTESTATION) == REOPEN

    correction = ledger.submit_correction(
        fact_id=att["fact_id"], field="result", new_value="加注量待核",
        reason="交接结论与计量不符", author="赵六", idem_key="reopen-1", shift_id="S2",
    )
    assert correction["mode"] == REOPEN
    assert ledger.shift_view("S2")["status"] == "reopen_pending"
    # 候选未签署前，对外仍然看到旧冻结版 v1。
    assert ledger.current_version("S2")["version"] == "v1"
    # 待签署期间不允许追加计量更正。
    fill = next(r for r in s2["records"] if r["record_type"] == FILL_TRANSACTION)
    with pytest.raises(HandoverError):
        ledger.submit_correction(
            fact_id=fill["fact_id"], field="mass_kg", new_value=6.0,
            reason="x", author="赵六", idem_key="amend-blocked", shift_id="S2",
        )

    v2 = ledger.sign_candidate("王五", "S2")
    assert v2["version"] == "v2"
    assert v2["parent_version"] == "v1"
    new_att = next(r for r in v2["records"] if r["record_type"] == SHIFT_ATTESTATION)
    assert new_att["values"]["result"] == "加注量待核"
    # 对外取数切到 v2；v1 仍是不可变历史。
    assert ledger.current_version("S2")["version"] == "v2"
    old = ledger.get_version("S2", "v1")
    assert next(r for r in old["records"] if r["record_type"] == SHIFT_ATTESTATION)["values"]["result"] == "正常"
    assert ledger.list_versions("S2")[0]["version"] == "v1"
    assert ledger.list_versions("S2")[1]["version"] == "v2"


def test_cancel_reopen_candidate_keeps_old_version(ledger):
    ledger.open_shift("S2")
    ledger.record_fact(SHIFT_ATTESTATION, {"result": "正常", "remark": ""})
    ledger.seal_shift("S2")
    ledger.sign_shift("王五", "S2")
    s2 = ledger.current_version("S2")
    att = s2["records"][0]
    ledger.submit_correction(
        fact_id=att["fact_id"], field="result", new_value="待核",
        reason="r", author="赵六", idem_key="reopen-1", shift_id="S2",
    )
    cancel = ledger.cancel_candidate(reason="复核后无需修改", author="赵六", shift_id="S2")
    assert cancel["cancels"]
    assert ledger.shift_view("S2")["status"] == "frozen"
    assert ledger.current_version("S2")["version"] == "v1"
    # 更正流水没有被删，撤销只是追加了一条标记。
    kinds = [item["kind"] for item in ledger._corrections_journal.read_all()]
    assert "correction" in kinds and "candidate_cancel" in kinds


def test_amend_folds_into_v2_when_reopen_signed(ledger):
    """v1 的追加更正在重开签署时进入 v2 基线。"""

    ledger.open_shift("S")
    ledger.record_fact(SHIFT_ATTESTATION, {"result": "正常", "remark": ""})
    ledger.record_fact(FILL_TRANSACTION, {"vehicle": "v9", "mass_kg": 5.0})
    ledger.seal_shift("S")
    ledger.sign_shift("甲", "S")
    s1 = ledger.current_version("S")
    fill = next(r for r in s1["records"] if r["record_type"] == FILL_TRANSACTION)
    ledger.submit_correction(
        fact_id=fill["fact_id"], field="mass_kg", new_value=6.0,
        reason="计量复核", author="乙", idem_key="a1", shift_id="S",
    )
    att = next(r for r in s1["records"] if r["record_type"] == SHIFT_ATTESTATION)
    ledger.submit_correction(
        fact_id=att["fact_id"], field="result", new_value="待核",
        reason="结论修正", author="乙", idem_key="r1", shift_id="S",
    )
    v2 = ledger.sign_candidate("甲", "S")
    new_fill = next(r for r in v2["records"] if r["record_type"] == FILL_TRANSACTION)
    # v2 基线已包含 v1 的追加更正（6.0），且不显示为待处理 change。
    assert new_fill["values"]["mass_kg"] == 6.0
    assert new_fill["changes"] == []


# --------------------------------------------------------------- 对外取数


def test_amend_after_v2_targets_v2_not_orphaned_on_v1(ledger):
    """重开签署出 v2 后，新的追加更正必须落在当前版 v2 并对外可见。"""

    ledger.open_shift("S")
    ledger.record_fact(SHIFT_ATTESTATION, {"result": "正常", "remark": ""})
    ledger.record_fact(FILL_TRANSACTION, {"vehicle": "v9", "mass_kg": 5.0})
    ledger.seal_shift("S")
    ledger.sign_shift("甲", "S")
    s1 = ledger.current_version("S")
    att = next(r for r in s1["records"] if r["record_type"] == SHIFT_ATTESTATION)
    ledger.submit_correction(
        fact_id=att["fact_id"], field="result", new_value="待核",
        reason="结论修正", author="乙", idem_key="r1", shift_id="S",
    )
    v2 = ledger.sign_candidate("甲", "S")
    fill = next(r for r in v2["records"] if r["record_type"] == FILL_TRANSACTION)

    correction = ledger.submit_correction(
        fact_id=fill["fact_id"], field="mass_kg", new_value=7.5,
        reason="再次复核", author="乙", idem_key="a2", shift_id="S",
    )
    assert correction["target_version"] == "v2"
    current = ledger.current_version("S")
    assert current["version"] == "v2"
    assert next(r for r in current["records"] if r["record_type"] == FILL_TRANSACTION)["values"]["mass_kg"] == 7.5
    # v1、v2 冻结内容都没被改写。
    assert next(r for r in ledger.get_version("S", "v2")["records"] if r["record_type"] == FILL_TRANSACTION)["values"]["mass_kg"] == 5.0


def test_external_read_never_sees_draft(ledger):
    view = ledger.open_shift()
    ledger.record_fact(FILL_TRANSACTION, {"mass_kg": 1.0})
    with pytest.raises(HandoverError):
        ledger.current_version()  # open
    ledger.seal_shift()
    with pytest.raises(HandoverError):
        ledger.current_version()  # sealed but not signed
    ledger.sign_shift("张三")
    current = ledger.current_version(view["shift_id"])
    assert current["version"] == "v1"
    # 草稿接口仍可取到未冻结内容（仅控制台管理用）。
    draft = ledger.draft_facts(view["shift_id"])
    assert draft["facts"][0]["values"]["mass_kg"] == 1.0


# --------------------------------------------------------------- 一致性核对


def test_reconcile_passes_on_clean_chain(platform):
    # 用带审计总线的 platform 走完整链路，核对三处一致。
    platform.handover.open_shift("S")
    platform.handover.record_fact(FILL_TRANSACTION, {"vehicle": "v", "mass_kg": 3.0})
    platform.handover.seal_shift("S")
    platform.handover.sign_shift("张三", "S")
    current = platform.handover.current_version("S")
    platform.handover_correct(
        fact_id=current["records"][0]["fact_id"], field="mass_kg", new_value=4.0,
        reason="复核", author="李四", idem_key="k1", shift_id="S",
    )
    report = platform.handover_reconcile("S")
    assert report["ok"] is True, report["problems"]
    assert report["versions"] == 1
    assert report["corrections"] == 1


def test_reconcile_detects_tampered_frozen_version(tmp_path, clock):
    from hydrorefuel.eventbus import EventBus

    bus = EventBus()
    first = HandoverLedger(str(tmp_path / "state"), SECRET, clock, bus=bus)
    first.open_shift("S")
    first.record_fact(FILL_TRANSACTION, {"mass_kg": 3.0})
    first.seal_shift("S")
    first.sign_shift("张三", "S")

    # 直接篡改只追加流水里的冻结数字，模拟磁盘被改。
    path = tmp_path / "state" / "handover_versions-00000.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[0]["facts"][0]["values"]["mass_kg"] = 999.0
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    reopened = HandoverLedger(str(tmp_path / "state"), SECRET, clock)
    report = reopened.reconcile("S")
    problems = {item["problem"] for item in report["problems"]}
    assert "content_digest_mismatch" in problems


def test_reconcile_detects_wrong_secret_signature(tmp_path, clock):
    first = HandoverLedger(str(tmp_path / "state"), SECRET, clock)
    first.open_shift("S")
    first.record_fact(FILL_TRANSACTION, {"mass_kg": 3.0})
    first.seal_shift("S")
    first.sign_shift("张三", "S")

    # 换一把密钥重开：签名验不过，说明签名人身份/密钥对不上。
    other = HandoverLedger(str(tmp_path / "state"), "attacker-secret", clock)
    report = other.reconcile("S")
    assert any(item["problem"] == "bad_signature" for item in report["problems"])


def test_reconcile_detects_missing_freeze_audit(ledger):
    # ledger 没有接审计总线：冻结/更正事件无处落审计，核对必须报缺审计。
    frozen = _frozen_shift(ledger)
    ledger.submit_correction(
        fact_id=frozen["records"][0]["fact_id"], field="mass_kg", new_value=11.0,
        reason="r", author="李四", idem_key="k",
    )
    report = ledger.reconcile(frozen["shift_id"], audit_entries=[])
    problems = {item["problem"] for item in report["problems"]}
    assert "missing_freeze_audit" in problems
    assert "missing_correction_audit" in problems


# --------------------------------------------------------------- 持久化重建


def test_state_rebuilds_from_append_only_journals(tmp_path, clock):
    first = HandoverLedger(str(tmp_path / "state"), SECRET, clock)
    view = first.open_shift("S")
    first.record_fact(FILL_TRANSACTION, {"mass_kg": 3.0})
    first.seal_shift("S")
    first.sign_shift("张三", "S")
    first.submit_correction(
        fact_id=first.current_version("S")["records"][0]["fact_id"],
        field="mass_kg", new_value=4.0, reason="r", author="李四", idem_key="k", shift_id="S",
    )

    reopened = HandoverLedger(str(tmp_path / "state"), SECRET, clock)
    current = reopened.current_version("S")
    assert current["records"][0]["values"]["mass_kg"] == 4.0
    assert reopened.shift_view("S")["status"] == "frozen"
    assert view["shift_id"] == "S"
