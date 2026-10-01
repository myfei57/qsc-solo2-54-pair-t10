"""班次交接台账：封口、签署冻结与更正。

台账是"班次交接"这一聚合的唯一写入口，背后是三条只追加 JSONL 流水：

- ``handover_facts``：当班记录的充装事实（一经封口不再写入）；
- ``handover_versions``：签署时冻结的版本（v1、v2……），含内容摘要与
  HMAC 签名，**永远不重写、不删除**；
- ``handover_corrections``：更正记录与候选撤销记录，含指纹与签名。

内存状态在构造时从三条流水重放重建，因此流水是唯一事实来源，不存在第二份
可被改写的"当前值"。状态机四态：

    open ──封口──▶ sealed ──签署──▶ frozen ──重开更正──▶ reopen_pending
                                     ▲                        │
                                     └──签署候选/撤销候选──────┘

冻结时点定死为"签署时"（见 :data:`~hydrorefuel.handover.policy.FREEZE_POINT`）：
班次末只封口，签署动作才冻结。更正方式按记录类型由策略表决定，调用方不能选。
"""

from __future__ import annotations

import threading
from typing import Iterable

from ..errors import HandoverError
from ..eventbus import Event, EventBus
from ..ids import IdFactory
from ..store.journal import JsonlJournal
from . import policy
from .signing import digest_content, sign_content, verify_signature

VERSION_PREFIX = "v"


class HandoverLedger:
    """班次交接的冻结版本与更正台账。"""

    def __init__(
        self,
        journal_root: str,
        secret: str,
        clock: object,
        bus: EventBus | None = None,
        segment_limit: int = 512,
    ) -> None:
        self._secret = secret
        self._clock = clock
        self._bus = bus
        self._facts_journal = JsonlJournal(journal_root, "handover_facts", segment_limit)
        self._versions_journal = JsonlJournal(journal_root, "handover_versions", segment_limit)
        self._corrections_journal = JsonlJournal(journal_root, "handover_corrections", segment_limit)
        self._fact_ids = IdFactory("fact")
        self._version_ids = IdFactory("ver")
        self._correction_ids = IdFactory("cor")
        self._shift_ids = IdFactory("shift")
        self._lock = threading.RLock()

        self._shifts: dict[str, dict] = {}
        self._facts: dict[str, list[dict]] = {}
        self._versions: dict[str, list[dict]] = {}
        self._corrections: dict[str, list[dict]] = {}
        self._active_id: str | None = None
        self._rebuild()

    # ------------------------------------------------------------------ 重建

    def _rebuild(self) -> None:
        """从三条只追加流水重放内存状态。"""

        for entry in self._facts_journal.read_all():
            self._facts.setdefault(entry["shift_id"], []).append(entry)
            self._touch_shift(entry["shift_id"], entry["ts"])
        for entry in self._versions_journal.read_all():
            self._versions.setdefault(entry["shift_id"], []).append(entry)
            shift = self._touch_shift(entry["shift_id"], entry["frozen_ts"])
            shift["status"] = "frozen"
            shift["pending_corrections"] = []
        for entry in self._corrections_journal.read_all():
            self._corrections.setdefault(entry["shift_id"], []).append(entry)
            if entry["kind"] == "correction" and entry["mode"] == policy.REOPEN:
                shift = self._shifts[entry["shift_id"]]
                if self._correction_state(entry) == "pending":
                    shift["status"] = "reopen_pending"
                    shift["pending_corrections"] = [entry["correction_id"]]
        for shift in self._shifts.values():
            if shift["status"] in ("open", "sealed"):
                self._active_id = shift["shift_id"]

    def _touch_shift(self, shift_id: str, ts: float) -> dict:
        shift = self._shifts.get(shift_id)
        if shift is None:
            shift = {
                "shift_id": shift_id,
                "opened_ts": ts,
                "sealed_ts": None,
                "status": "open",
                "pending_corrections": [],
            }
            self._shifts[shift_id] = shift
        return shift

    # ------------------------------------------------------------- 班次/事实

    def open_shift(self, shift_id: str | None = None) -> dict:
        """开班。同一时刻只允许一个未完成（未冻结）的班次。"""

        with self._lock:
            if self._active_id is not None:
                active = self._shifts[self._active_id]
                if active["status"] != "frozen":
                    raise HandoverError(
                        f"上一班 {self._active_id} 尚未签署冻结，不能开班", component="handover"
                    )
            shift_id = shift_id or self._shift_ids.next()
            if shift_id in self._shifts:
                raise HandoverError(f"班次 {shift_id} 已存在", component="handover")
            now = self._clock.now()
            self._shifts[shift_id] = {
                "shift_id": shift_id,
                "opened_ts": now,
                "sealed_ts": None,
                "status": "open",
                "pending_corrections": [],
            }
            self._facts[shift_id] = []
            self._active_id = shift_id
            self._publish("shift_opened", shift_id=shift_id, opened_ts=round(now, 3))
            return self.shift_view(shift_id)

    def record_fact(
        self,
        record_type: str,
        values: dict,
        *,
        shift_id: str | None = None,
    ) -> dict:
        """在当班草稿里记一条充装事实；封口后拒绝再记。"""

        if record_type not in policy.RECORD_TYPES:
            raise HandoverError(f"未知记录类型: {record_type!r}", component="handover")
        if not isinstance(values, dict) or not values:
            raise HandoverError("充装事实必须携带非空字段", component="handover")
        with self._lock:
            shift = self._require_shift(self._resolve_shift(shift_id))
            if shift["status"] != "open":
                raise HandoverError("班次已封口，不能再记入事实", component="handover")
            entry = {
                "kind": "fact",
                "fact_id": self._fact_ids.next(),
                "shift_id": shift["shift_id"],
                "ts": round(self._clock.now(), 3),
                "record_type": record_type,
                "values": dict(values),
            }
            self._facts_journal.append(entry)
            self._facts.setdefault(shift["shift_id"], []).append(entry)
            return entry

    def seal_shift(self, shift_id: str | None = None) -> dict:
        """班次末封口：停止再记事实，但此刻**不**冻结，等待签署。"""

        with self._lock:
            shift = self._require_shift(self._resolve_shift(shift_id))
            if shift["status"] != "open":
                raise HandoverError("只有进行中的班次可以封口", component="handover")
            if not self._facts.get(shift["shift_id"]):
                raise HandoverError("空班次不能封口交接", component="handover")
            shift["status"] = "sealed"
            shift["sealed_ts"] = round(self._clock.now(), 3)
            self._publish("shift_sealed", shift_id=shift["shift_id"], sealed_ts=shift["sealed_ts"])
            return self.shift_view(shift["shift_id"])

    # ------------------------------------------------------------- 签署冻结

    def sign_shift(self, signer: str, shift_id: str | None = None) -> dict:
        """签署封口班次：**此刻**把充装事实冻成不可变 v1。"""

        if not signer:
            raise HandoverError("签署人不能为空", component="handover")
        with self._lock:
            shift = self._require_shift(self._resolve_shift(shift_id))
            if shift["status"] != "sealed":
                raise HandoverError("班次必须先封口（班次末）再签署冻结", component="handover")
            version = self._freeze_version(shift, signer, parent=None, source_corrections=[])
            self._active_id = None
            return self._version_view(version)

    def _freeze_version(
        self,
        shift: dict,
        signer: str,
        *,
        parent: dict | None,
        source_corrections: list[dict],
    ) -> dict:
        """生成一个冻结版本：定稿事实 → 摘要 → 签名 → 落只追加流水。"""

        shift_id = shift["shift_id"]
        facts, folded = self._snapshot_facts(shift_id, parent, source_corrections)
        version_no = 1 if parent is None else parent["version_no"] + 1
        snapshot = {
            "shift_id": shift_id,
            "version_no": version_no,
            "parent_version": None if parent is None else self._label(parent),
            "facts": facts,
        }
        content_digest = digest_content(snapshot)
        entry = {
            "kind": "version",
            "version_id": self._version_ids.next(),
            "shift_id": shift_id,
            "version_no": version_no,
            "frozen_ts": round(self._clock.now(), 3),
            "freeze_point": policy.FREEZE_POINT,
            "signer": signer,
            "facts": facts,
            "parent_version": snapshot["parent_version"],
            # 重开签署时，父版已生效的追加更正折进本版基线，并在此留痕。
            "folded_corrections": folded,
            "source_corrections": [item["correction_id"] for item in source_corrections],
            "content_digest": content_digest,
            "signature": sign_content(signer, content_digest, self._secret),
        }
        self._versions_journal.append(entry)
        self._versions.setdefault(shift_id, []).append(entry)
        shift["status"] = "frozen"
        shift["pending_corrections"] = []
        self._publish(
            "handover_frozen",
            shift_id=shift_id,
            version=self._label(entry),
            version_id=entry["version_id"],
            content_digest=content_digest,
            signer=signer,
        )
        return entry

    def _snapshot_facts(
        self,
        shift_id: str,
        parent: dict | None,
        source_corrections: list[dict],
    ) -> tuple[list[dict], list[str]]:
        """定稿新版本的事实快照，返回（事实行，折进的追加更正 id）。

        v1 直接取当班草稿；重开版本以**父版当前有效值**（父版冻结值叠加
        面向父版的追加更正）为基线，再叠加本次重开改动。
        """

        if parent is None:
            rows = [
                {
                    "fact_id": item["fact_id"],
                    "ts": item["ts"],
                    "record_type": item["record_type"],
                    "values": dict(item["values"]),
                }
                for item in self._facts.get(shift_id, [])
            ]
            folded: list[str] = []
        else:
            effective = self._effective_values(parent)
            rows = [
                {
                    "fact_id": item["fact_id"],
                    "ts": item["ts"],
                    "record_type": item["record_type"],
                    "values": dict(effective[item["fact_id"]]),
                }
                for item in parent["facts"]
            ]
            folded = [item["correction_id"] for item in self._active_amendments(shift_id, self._label(parent))]
        values_by_id = {row["fact_id"]: row["values"] for row in rows}
        for correction in source_corrections:
            values_by_id[correction["fact_id"]][correction["field"]] = correction["new_value"]
        return sorted(rows, key=lambda row: row["fact_id"]), folded

    # ----------------------------------------------------------------- 更正

    def submit_correction(
        self,
        *,
        fact_id: str,
        field: str,
        new_value: object,
        reason: str,
        author: str,
        idem_key: str,
        shift_id: str | None = None,
    ) -> dict:
        """对冻结版提交一条更正；方式由事实类型决定，提交天然幂等。"""

        if not reason:
            raise HandoverError("更正必须写明理由", component="handover")
        if not author:
            raise HandoverError("更正必须有更正人签名", component="handover")
        if not idem_key:
            raise HandoverError("更正必须携带幂等键 idem_key", component="handover")
        with self._lock:
            shift = self._require_shift(self._resolve_shift(shift_id))
            if shift["status"] == "reopen_pending":
                raise HandoverError("存在待签署的重开版本，请先签署或撤销", component="handover")
            if shift["status"] != "frozen":
                raise HandoverError("只能对冻结版本提出更正", component="handover")
            # 更正只允许落在当前冻结版：fact_id 在当前版找不到直接拒绝，
            # 因此重开签署出 v2 后不可能再把更正挂到对外不可见的 v1 上。
            current = self._latest_version(shift["shift_id"])
            record = self._fact_in_version(current, fact_id)
            record_type = record["record_type"]
            mode = policy.correction_mode(record_type)
            if not policy.is_correctable(record_type, field):
                raise HandoverError(f"{record_type} 不允许更正字段 {field!r}", component="handover")

            # 幂等判定必须先于"原值=新值"拦截：首条更正生效后，重试请求的新值
            # 已是当前值。匹配只比对客户端提交字段，不含派生的 old_value。
            existing = self._find_idempotent(shift["shift_id"], idem_key)
            if existing is not None:
                same_request = (
                    existing["fact_id"] == fact_id
                    and existing["field"] == field
                    and existing["new_value"] == new_value
                    and existing["reason"] == reason
                    and existing["mode"] == mode
                )
                if not same_request:
                    raise HandoverError(
                        f"幂等键 {idem_key} 已用于另一条更正，内容冲突", component="handover"
                    )
                return {**existing, "idempotent": True}

            effective = self._effective_values(current)
            if field not in effective[fact_id]:
                raise HandoverError(f"事实 {fact_id} 上没有字段 {field!r}", component="handover")
            old_value = effective[fact_id][field]
            if new_value == old_value:
                raise HandoverError("更正值与当前值相同，无需更正", component="handover")

            fingerprint = self._fingerprint(
                shift["shift_id"], mode, self._label(current), fact_id, field, old_value, new_value, reason
            )
            entry = {
                "kind": "correction",
                "correction_id": self._correction_ids.next(),
                "shift_id": shift["shift_id"],
                "idem_key": idem_key,
                "mode": mode,
                "target_version": self._label(current),
                "fact_id": fact_id,
                "record_type": record_type,
                "field": field,
                "old_value": old_value,
                "new_value": new_value,
                "reason": reason,
                "author": author,
                "ts": round(self._clock.now(), 3),
                "fingerprint": fingerprint,
                "signature": sign_content(author, fingerprint, self._secret),
            }
            self._corrections_journal.append(entry)
            self._corrections.setdefault(shift["shift_id"], []).append(entry)
            if mode == policy.REOPEN:
                shift["status"] = "reopen_pending"
                shift["pending_corrections"] = [entry["correction_id"]]
            self._publish(
                "handover_corrected",
                shift_id=shift["shift_id"],
                correction_id=entry["correction_id"],
                idem_key=idem_key,
                mode=mode,
                target_version=self._label(current),
                fact_id=fact_id,
                field=field,
                old_value=old_value,
                new_value=new_value,
                fingerprint=fingerprint,
                author=author,
            )
            return {**entry, "idempotent": False}

    def sign_candidate(self, signer: str, shift_id: str | None = None) -> dict:
        """签署重开候选：在父冻结版（含既有追加更正）上冻成新版本。"""

        if not signer:
            raise HandoverError("签署人不能为空", component="handover")
        with self._lock:
            shift = self._require_shift(self._resolve_shift(shift_id))
            if shift["status"] != "reopen_pending":
                raise HandoverError("该班次没有待签署的重开版本", component="handover")
            pending = [
                item
                for item in self._corrections.get(shift["shift_id"], [])
                if item["kind"] == "correction"
                and item["mode"] == policy.REOPEN
                and item["correction_id"] in shift["pending_corrections"]
            ]
            parent = self._latest_version(shift["shift_id"])
            version = self._freeze_version(shift, signer, parent=parent, source_corrections=pending)
            return self._version_view(version)

    def cancel_candidate(
        self, *, reason: str, author: str, shift_id: str | None = None
    ) -> dict:
        """撤销待签署候选。更正记录本身不可删，只追加一条撤销标记。"""

        if not reason:
            raise HandoverError("撤销候选必须写明理由", component="handover")
        if not author:
            raise HandoverError("撤销必须有操作人", component="handover")
        with self._lock:
            shift = self._require_shift(self._resolve_shift(shift_id))
            if shift["status"] != "reopen_pending":
                raise HandoverError("该班次没有待签署的重开版本", component="handover")
            entry = {
                "kind": "candidate_cancel",
                "correction_id": self._correction_ids.next(),
                "shift_id": shift["shift_id"],
                "cancels": list(shift["pending_corrections"]),
                "reason": reason,
                "author": author,
                "ts": round(self._clock.now(), 3),
            }
            self._corrections_journal.append(entry)
            self._corrections.setdefault(shift["shift_id"], []).append(entry)
            shift["status"] = "frozen"
            shift["pending_corrections"] = []
            self._publish(
                "handover_candidate_cancelled",
                shift_id=shift["shift_id"],
                cancels=entry["cancels"],
                author=author,
            )
            return entry

    # ----------------------------------------------------------------- 取数

    def shift_view(self, shift_id: str) -> dict:
        """班次状态视图（含草稿；控制台管理页用，不对外取数）。"""

        with self._lock:
            shift = self._require_shift(shift_id)
            view = dict(shift)
            versions = self._versions.get(shift_id, [])
            view["versions"] = [self._label(item) for item in versions]
            view["fact_count"] = len(self._facts.get(shift_id, []))
            view["freeze_point"] = policy.FREEZE_POINT
            return view

    def list_shifts(self) -> list[dict]:
        """全部班次（按开班时间升序）。"""

        with self._lock:
            return [self.shift_view(item) for item in sorted(self._shifts, key=lambda s: self._shifts[s]["opened_ts"])]

    def draft_facts(self, shift_id: str | None = None) -> dict:
        """读取封口前的草稿事实；对外取数不走这里。"""

        with self._lock:
            shift = self._require_shift(self._resolve_shift(shift_id))
            return {
                "shift_id": shift["shift_id"],
                "status": shift["status"],
                "facts": [dict(item) for item in self._facts.get(shift["shift_id"], [])],
            }

    def list_versions(self, shift_id: str) -> list[dict]:
        """列出一个班次的全部冻结版本（历史版本不可变，都可取）。"""

        with self._lock:
            self._require_shift(shift_id)
            return [self._version_view(item) for item in self._versions.get(shift_id, [])]

    def get_version(self, shift_id: str, label: str) -> dict:
        """取指定冻结版本（如 v1）；取的永远是冻结那一版。"""

        with self._lock:
            self._require_shift(shift_id)
            return self._version_view(self._find_version(shift_id, label))

    def current_version(self, shift_id: str | None = None) -> dict:
        """对外取数入口：返回当前冻结版叠加追加更正后的有效值。

        草稿、待签署候选一律不返回；没有冻结版直接报错，确保外部永远
        看不到"签署前的数"。
        """

        with self._lock:
            shift = self._require_shift(self._resolve_shift(shift_id))
            if shift["status"] not in ("frozen", "reopen_pending"):
                raise HandoverError("该班次尚无冻结版本，外部不可取数", component="handover")
            return self._version_view(self._latest_version(shift["shift_id"]), effective=True)

    def _version_view(self, version: dict, *, effective: bool = False) -> dict:
        """组装版本视图；``effective`` 时叠加面向该版的追加更正。"""

        shift_id = version["shift_id"]
        records: dict[str, dict] = {}
        for fact in version["facts"]:
            records[fact["fact_id"]] = {
                "fact_id": fact["fact_id"],
                "ts": fact["ts"],
                "record_type": fact["record_type"],
                "values": dict(fact["values"]),
                "changes": [],
            }
        amended = False
        # 只有"对外取当前版"（effective=True）才叠加面向该版的追加更正；
        # 直接取历史版本时一律返回冻那一刻的原样内容。
        if effective:
            for correction in self._active_amendments(shift_id, self._label(version)):
                row = records.get(correction["fact_id"])
                if row is None:
                    continue
                row["values"][correction["field"]] = correction["new_value"]
                row["changes"].append(
                    {
                        "correction_id": correction["correction_id"],
                        "field": correction["field"],
                        "old_value": correction["old_value"],
                        "new_value": correction["new_value"],
                        "reason": correction["reason"],
                        "author": correction["author"],
                        "ts": correction["ts"],
                    }
                )
                amended = True
        return {
            "shift_id": shift_id,
            "version": self._label(version),
            "version_id": version["version_id"],
            "version_no": version["version_no"],
            "parent_version": version["parent_version"],
            "folded_corrections": list(version.get("folded_corrections", [])),
            "frozen_ts": version["frozen_ts"],
            "freeze_point": version["freeze_point"],
            "signer": version["signer"],
            "content_digest": version["content_digest"],
            "signature": version["signature"],
            "immutable": True,
            "amended": amended,
            "records": [records[key] for key in sorted(records)],
        }

    # ------------------------------------------------------------- 一致性核对

    def reconcile(self, shift_id: str | None = None, audit_entries: Iterable[dict] | None = None) -> dict:
        """核对冻结、更正、审计三处一致性，返回问题清单与统计。"""

        with self._lock:
            targets = [self._resolve_shift(shift_id)] if shift_id else list(self._shifts)
            audit = list(audit_entries or [])
            problems: list[dict] = []
            for sid in targets:
                if sid not in self._shifts:
                    raise HandoverError(f"未知班次 {sid}", component="handover")
                self._reconcile_shift(sid, audit, problems)
            return {
                "ok": not problems,
                "freeze_point": policy.FREEZE_POINT,
                "shifts_checked": len(targets),
                "versions": sum(len(self._versions.get(sid, [])) for sid in targets),
                "corrections": sum(
                    1
                    for sid in targets
                    for item in self._corrections.get(sid, [])
                    if item["kind"] == "correction"
                ),
                "problems": problems,
            }

    def _reconcile_shift(self, shift_id: str, audit: list[dict], problems: list[dict]) -> None:
        versions = self._versions.get(shift_id, [])
        corrections = [
            item for item in self._corrections.get(shift_id, []) if item["kind"] == "correction"
        ]

        # 1) 冻结版本：重算内容摘要、验签、核对冻结时点口径，并找审计事件。
        for version in versions:
            snapshot = {
                "shift_id": shift_id,
                "version_no": version["version_no"],
                "parent_version": version["parent_version"],
                "facts": version["facts"],
            }
            if digest_content(snapshot) != version["content_digest"]:
                problems.append({"scope": "version", "version": self._label(version), "problem": "content_digest_mismatch"})
            if not verify_signature(version["signer"], version["content_digest"], self._secret, version["signature"]):
                problems.append({"scope": "version", "version": self._label(version), "problem": "bad_signature"})
            if version["freeze_point"] != policy.FREEZE_POINT:
                problems.append({"scope": "version", "version": self._label(version), "problem": "freeze_point_changed"})
            if not self._has_audit(audit, "handover_frozen", {"version_id": version["version_id"], "content_digest": version["content_digest"]}):
                problems.append({"scope": "audit", "version": self._label(version), "problem": "missing_freeze_audit"})

        # 2) 更正：重算指纹、验签、核对原值与目标事实，并找审计事件。
        for correction in corrections:
            if self._fingerprint(
                shift_id,
                correction["mode"],
                correction["target_version"],
                correction["fact_id"],
                correction["field"],
                correction["old_value"],
                correction["new_value"],
                correction["reason"],
            ) != correction["fingerprint"]:
                problems.append({"scope": "correction", "correction_id": correction["correction_id"], "problem": "fingerprint_mismatch"})
            if not verify_signature(correction["author"], correction["fingerprint"], self._secret, correction["signature"]):
                problems.append({"scope": "correction", "correction_id": correction["correction_id"], "problem": "bad_signature"})
            if not self._has_audit(audit, "handover_corrected", {"correction_id": correction["correction_id"], "fingerprint": correction["fingerprint"]}):
                problems.append({"scope": "audit", "correction_id": correction["correction_id"], "problem": "missing_correction_audit"})
            self._check_correction_target(correction, versions, problems)

        # 3) 幂等键：同一班次内同一幂等键只能对应同一指纹。
        seen: dict[str, str] = {}
        for correction in corrections:
            if self._correction_state(correction) == "cancelled":
                continue
            prior = seen.get(correction["idem_key"])
            if prior is not None and prior != correction["fingerprint"]:
                problems.append({"scope": "correction", "correction_id": correction["correction_id"], "problem": "idem_key_conflict"})
            seen.setdefault(correction["idem_key"], correction["fingerprint"])

    def _check_correction_target(self, correction: dict, versions: list[dict], problems: list[dict]) -> None:
        target = next((item for item in versions if self._label(item) == correction["target_version"]), None)
        if target is None:
            problems.append({"scope": "correction", "correction_id": correction["correction_id"], "problem": "target_version_missing"})
            return
        fact = next((item for item in target["facts"] if item["fact_id"] == correction["fact_id"]), None)
        if fact is None:
            problems.append({"scope": "correction", "correction_id": correction["correction_id"], "problem": "fact_missing_in_target"})
            return
        # 更正提交时的原值必须等于"目标版本快照按流水顺序叠加本条之前的
        # 同版追加更正"后的值；重开更正则直接比对目标版有效值。
        if correction["mode"] == policy.AMEND:
            before = fact["values"].get(correction["field"])
            for earlier in self._active_amendments(correction["shift_id"], correction["target_version"]):
                if earlier["correction_id"] == correction["correction_id"]:
                    break
                if earlier["fact_id"] == correction["fact_id"] and earlier["field"] == correction["field"]:
                    before = earlier["new_value"]
        else:
            before = self._effective_values(target).get(correction["fact_id"], {}).get(correction["field"])
        if correction["old_value"] != before:
            problems.append({"scope": "correction", "correction_id": correction["correction_id"], "problem": "old_value_mismatch"})

    @staticmethod
    def _has_audit(entries: list[dict], action: str, payload: dict) -> bool:
        for entry in entries:
            if entry.get("action") != action:
                continue
            audit_payload = entry.get("payload", {})
            if all(audit_payload.get(key) == value for key, value in payload.items()):
                return True
        return False

    # ----------------------------------------------------------------- 辅助

    def _resolve_shift(self, shift_id: str | None) -> str:
        if shift_id is not None:
            return shift_id
        if self._active_id is not None:
            return self._active_id
        # 没有进行中（开/封/待签署）的班次时，默认最近创建的班次，便于对刚
        # 签署冻结的这一班直接提更正；一旦下一班开班，默认即指向下一班，对
        # 历史班的操作必须显式带 shift_id。
        if not self._shifts:
            raise HandoverError("没有进行中的班次，请先开班", component="handover")
        return max(self._shifts, key=lambda sid: self._shifts[sid]["opened_ts"])

    def _require_shift(self, shift_id: str) -> dict:
        shift = self._shifts.get(shift_id)
        if shift is None:
            raise HandoverError(f"未知班次 {shift_id}", component="handover")
        return shift

    def _latest_version(self, shift_id: str) -> dict:
        versions = self._versions.get(shift_id, [])
        if not versions:
            raise HandoverError("该班次尚无冻结版本", component="handover")
        return versions[-1]

    def _find_version(self, shift_id: str, label: str) -> dict:
        for item in self._versions.get(shift_id, []):
            if self._label(item) == label:
                return item
        raise HandoverError(f"班次 {shift_id} 没有版本 {label}", component="handover")

    @staticmethod
    def _label(version: dict) -> str:
        return f"{VERSION_PREFIX}{version['version_no']}"

    @staticmethod
    def _fact_in_version(version: dict, fact_id: str) -> dict:
        fact = next((item for item in version["facts"] if item["fact_id"] == fact_id), None)
        if fact is None:
            raise HandoverError(f"冻结版中没有事实 {fact_id}", component="handover")
        return fact

    def _active_amendments(self, shift_id: str, label: str) -> list[dict]:
        """面向某版本生效的追加更正（按流水顺序）。"""

        return [
            item
            for item in self._corrections.get(shift_id, [])
            if item["kind"] == "correction"
            and item["mode"] == policy.AMEND
            and item["target_version"] == label
        ]

    def _effective_values(self, version: dict) -> dict[str, dict]:
        """某版本叠加其追加更正后的当前有效值。"""

        values = {fact["fact_id"]: dict(fact["values"]) for fact in version["facts"]}
        for correction in self._active_amendments(version["shift_id"], self._label(version)):
            if correction["fact_id"] in values:
                values[correction["fact_id"]][correction["field"]] = correction["new_value"]
        return values

    def _correction_state(self, correction: dict) -> str:
        """重开更正的生命周期状态：pending / absorbed / cancelled。"""

        if correction["mode"] != policy.REOPEN:
            return "applied"
        shift_id = correction["shift_id"]
        for entry in self._corrections.get(shift_id, []):
            if entry["kind"] == "candidate_cancel" and correction["correction_id"] in entry["cancels"]:
                return "cancelled"
        for version in self._versions.get(shift_id, []):
            if correction["correction_id"] in version["source_corrections"]:
                return "absorbed"
        return "pending"

    def _find_idempotent(self, shift_id: str, idem_key: str) -> dict | None:
        """按幂等键找一条仍生效的更正（已撤销的不算，允许重新提交）。"""

        for item in self._corrections.get(shift_id, []):
            if item["kind"] != "correction" or item["idem_key"] != idem_key:
                continue
            if self._correction_state(item) == "cancelled":
                continue
            return item
        return None

    @staticmethod
    def _fingerprint(
        shift_id: str,
        mode: str,
        target_version: str,
        fact_id: str,
        field: str,
        old_value: object,
        new_value: object,
        reason: str,
    ) -> str:
        return digest_content(
            {
                "shift_id": shift_id,
                "mode": mode,
                "target_version": target_version,
                "fact_id": fact_id,
                "field": field,
                "old_value": old_value,
                "new_value": new_value,
                "reason": reason,
            }
        )

    def _publish(self, action: str, **payload: object) -> None:
        if self._bus is not None:
            self._bus.publish(Event.of("handover", "handover", action, **payload))
