"""班次交接台账：草稿、签署冻结、追加更正、重开一版。

台账只持有一份活动草稿（``shift_active_draft`` 文档），冻结后草稿清空；冻结
版写成一次性文档（``hvr-<version_id>``，文档已存在即拒绝写入），历史版本永不
覆盖、永不删除。更正走独立的 ``corrections`` 追加流水，每条都记新旧值、理由
与更正人；重开分两步——先基于被质疑版本开一个携带"更正指令"的子版本草稿，
重新签署冻结时才落下 reopen 更正流水。

冻结、更正、重开的每个动作都通过事件总线发事件，由审计订阅者落审计流水，因
此"冻结件 / 更正流水 / 审计流水"三处可以互相对账（见 :meth:`ShiftBook.reconcile`）。
"""

from __future__ import annotations

import copy
import hmac
import threading
import uuid
from typing import Iterable

from ..clock import Clock
from ..errors import InterlockError, StorageError
from ..eventbus import Event, EventBus
from ..store.jsonfile import JsonStore
from ..store.journal import JsonlJournal
from . import model
from .policy import FREEZE_POINT, KNOWN_TYPES, require_kind

DRAFT_DOC = "shift_active_draft"
KEY_DOC = "shift_signing_key"
CORRECTION_TOPIC = "corrections"


class ShiftBook:
    """班次交接的签署与更正台账。"""

    def __init__(self, clock: Clock, bus: EventBus, store: JsonStore) -> None:
        self._clock = clock
        self._bus = bus
        self._store = store
        self._corrections_journal = JsonlJournal(store.root, CORRECTION_TOPIC, segment_limit=512)
        self._lock = threading.RLock()
        self._key = self._load_key()
        self._active: dict | None = self._load_draft()
        self._frozen: dict[str, dict] = {}
        self._by_shift: dict[str, list[str]] = {}
        self._corrections: list[dict] = []
        self._idem: dict[str, dict] = {}
        self._load_frozen()
        self._load_corrections()

    # ------------------------------------------------------------------ 装配

    def _load_key(self) -> bytes:
        raw = self._store.read(KEY_DOC)
        if isinstance(raw, dict) and raw.get("key"):
            return str(raw["key"]).encode("utf-8")
        key = model.new_signing_key()
        self._store.write(KEY_DOC, {"key": key, "alg": "HMAC-SHA256", "created_ts": round(self._clock.now(), 3)})
        return key.encode("utf-8")

    def _load_draft(self) -> dict | None:
        draft = self._store.read(DRAFT_DOC)
        return dict(draft) if isinstance(draft, dict) else None

    def _load_frozen(self) -> None:
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("hvr-"):
                continue
            body = self._store.read(doc_id)
            if not isinstance(body, dict):
                continue
            version_id = str(body.get("version_id", ""))
            self._frozen[version_id] = body
            self._by_shift.setdefault(str(body.get("shift_id")), []).append(version_id)
        for versions in self._by_shift.values():
            versions.sort(key=lambda vid: self._frozen[vid]["seq"])

    def _load_corrections(self) -> None:
        for item in self._corrections_journal.read_all():
            self._corrections.append(item)
            key = item.get("idempotency_key")
            if key:
                self._idem[str(key)] = item

    def _persist_draft(self) -> None:
        if self._active is None:
            self._store.remove(DRAFT_DOC)
        else:
            self._store.write(DRAFT_DOC, self._active)

    # ------------------------------------------------------------------ 视图

    def summary(self) -> dict:
        """台账摘要，供控制台与状态快照展示。"""

        with self._lock:
            latest = self._latest_version_id()
            return {
                "freeze_point": FREEZE_POINT,
                "active_draft": self._draft_view() if self._active else None,
                "frozen_versions": len(self._frozen),
                "shifts": len(self._by_shift),
                "corrections": len(self._corrections),
                "latest_version_id": latest,
                "latest_shift_id": self._frozen[latest]["shift_id"] if latest else None,
            }

    def _draft_view(self) -> dict:
        assert self._active is not None
        return {
            "shift_id": self._active["shift_id"],
            "seq": self._active["seq"],
            "parent_version_id": self._active.get("parent_version_id"),
            "draft_kind": self._active["draft_kind"],
            "station": self._active["station"],
            "started_ts": self._active["started_ts"],
            "facts": copy.deepcopy(self._active["facts"]),
            "reopen": copy.deepcopy(self._active.get("source")) if self._active["draft_kind"] == "reopen" else None,
        }

    def active_draft(self) -> dict | None:
        """返回活动草稿；没有草稿时为 ``None``（对外绝不把草稿当交接件）。"""

        with self._lock:
            return self._draft_view() if self._active else None

    def list_versions(self, shift_id: str | None = None) -> list[dict]:
        """列出冻结版（按班次、版本号升序）。"""

        with self._lock:
            if shift_id is not None:
                ids = list(self._by_shift.get(shift_id, ()))
            else:
                ids = [vid for versions in self._by_shift.values() for vid in versions]
                ids.sort(key=lambda vid: (self._frozen[vid]["shift_id"], self._frozen[vid]["seq"]))
            return [self._version_header(self._frozen[vid]) for vid in ids]

    def get_version(self, version_id: str) -> dict:
        """读取一份完整冻结版（历史不可变原件）。"""

        with self._lock:
            return copy.deepcopy(self._require_version(version_id))

    def corrections(self, version_id: str | None = None) -> list[dict]:
        """列出更正流水，可按目标版本过滤。"""

        with self._lock:
            items = self._corrections if version_id is None else [
                item for item in self._corrections if item.get("version_id") == version_id
            ]
            return copy.deepcopy(items)

    # ------------------------------------------------------------------ 开班

    def open_shift(self, station: str, manual_facts: Iterable[dict] | None = None) -> dict:
        """开一个新班次草稿；同一时刻只允许一个活动草稿。"""

        station = str(station or "").strip()
        if not station:
            raise InterlockError("开班必须提供工位号 station", component="shift")
        with self._lock:
            if self._active is not None:
                raise InterlockError(
                    f"已有活动班次 {self._active['shift_id']}（{self._active['draft_kind']}）未冻结，不能再开班",
                    component="shift",
                )
            shift_id = f"shift-{uuid.uuid4().hex[:12]}"
            self._active = {
                "shift_id": shift_id,
                "seq": 1,
                "parent_version_id": None,
                "draft_kind": "shift",
                "started_ts": round(self._clock.now(), 3),
                "station": station,
                "facts": [],
            }
            for item in manual_facts or ():
                self._append_manual_fact(item)
            self._persist_draft()
            self._publish(
                "shift_opened",
                shift_id=self._active["shift_id"],
                seq=1,
                station=station,
                fact_count=len(self._active["facts"]),
            )
            return self._draft_view()

    def record_fact(self, record_type: str, data: dict, op_id: str | None = None) -> dict | None:
        """把一条设备计量事实记入活动班次草稿（卸氢、加注自动调用）。

        没有活动班次或当前是重开版草稿时返回 ``None``（现场操作不因交接台账
        状态而被拦住）；开班正常时返回落入草稿的事实。
        """

        with self._lock:
            if self._active is None or self._active["draft_kind"] != "shift":
                return None
            record_type = self._require_known_type(record_type)
            fact = {
                "fact_id": f"f{len(self._active['facts']) + 1:03d}",
                "record_type": record_type,
                "ts": round(self._clock.now(), 3),
                "op_id": op_id,
                "data": {key: value for key, value in dict(data).items() if _is_primitive(value)},
            }
            self._active["facts"].append(fact)
            self._persist_draft()
            self._publish(
                "shift_fact_recorded",
                station=self._active["station"],
                shift_id=self._active["shift_id"],
                fact_id=fact["fact_id"],
                record_type=record_type,
                op_id=op_id or "",
            )
            return copy.deepcopy(fact)

    def _append_manual_fact(self, raw: dict) -> dict:
        assert self._active is not None
        record_type = self._require_known_type(str(raw.get("record_type", "")))
        data = dict(raw.get("data", {}))
        fact = {
            "fact_id": f"f{len(self._active['facts']) + 1:03d}",
            "record_type": record_type,
            "ts": round(self._clock.now(), 3),
            "op_id": None,
            "data": {key: value for key, value in data.items() if _is_primitive(value)},
        }
        self._active["facts"].append(fact)
        return fact

    # ------------------------------------------------------------------ 冻结

    def freeze(self, signers: list[str], manual_facts: Iterable[dict] | None = None) -> dict:
        """签署并冻结活动草稿。冻结点定死为签署时刻。"""

        names = _clean_signers(signers)
        with self._lock:
            if self._active is None:
                raise InterlockError("没有活动草稿，无法签署冻结", component="shift")
            for item in manual_facts or ():
                if self._active["draft_kind"] != "shift":
                    raise InterlockError("重开版草稿不允许在冻结时追加新事实", component="shift")
                self._append_manual_fact(item)
            if not self._active["facts"]:
                raise InterlockError("空班次不能冻结：至少要有一条交接事实", component="shift")

            draft = self._active
            now = round(self._clock.now(), 3)
            frozen = {
                "version_id": f"hvr-{uuid.uuid4().hex[:12]}",
                "shift_id": draft["shift_id"],
                "seq": draft["seq"],
                "parent_version_id": draft.get("parent_version_id"),
                "freeze_point": FREEZE_POINT,
                "frozen_ts": now,
                "started_ts": draft["started_ts"],
                "draft_kind": draft["draft_kind"],
                "station": draft["station"],
                "facts": copy.deepcopy(draft["facts"]),
            }
            frozen["fingerprint"] = model.content_fingerprint(frozen)
            frozen["signatures"] = model.make_signatures(frozen["fingerprint"], names, self._key, now)

            doc_id = frozen["version_id"]
            if self._store.exists(doc_id):  # 一次性文档：撞标识必须停手，绝不覆盖。
                raise StorageError(f"冻结版文档 {doc_id} 已存在，拒绝覆盖", component="shift")
            self._store.write(doc_id, frozen)
            self._index_frozen(frozen)

            correction: dict | None = None
            if draft["draft_kind"] == "reopen":
                correction = self._close_reopen(draft, frozen, now)

            self._active = None
            self._persist_draft()
            self._publish(
                "shift_frozen",
                station=frozen["station"],
                shift_id=frozen["shift_id"],
                version_id=frozen["version_id"],
                seq=frozen["seq"],
                parent_version_id=frozen["parent_version_id"] or "",
                freeze_point=FREEZE_POINT,
                fact_count=len(frozen["facts"]),
                fingerprint=frozen["fingerprint"],
                signers=list(names),
            )
            return {"version": copy.deepcopy(frozen), "correction": correction}

    def _close_reopen(self, draft: dict, frozen: dict, now: float) -> dict:
        source = draft["source"]
        parent = self._require_version(source["base_version_id"])
        old_fact = self._effective_fact(parent["version_id"], source["fact_id"])
        new_fact = self._require_fact(frozen, source["fact_id"])
        field_changes = {
            field: {"old": old_fact["data"].get(field), "new": new_fact["data"].get(field)}
            for field in source["changes"]
        }
        correction = {
            "correction_id": f"cor-{uuid.uuid4().hex[:12]}",
            "idempotency_key": source.get("idempotency_key") or "",
            "kind": "reopen",
            "shift_id": frozen["shift_id"],
            "version_id": parent["version_id"],
            "new_version_id": frozen["version_id"],
            "fact_id": source["fact_id"],
            "record_type": source["record_type"],
            "field_changes": field_changes,
            "reason": source["reason"],
            "author": source["author"],
            "ts": now,
        }
        self._append_correction(correction)
        self._publish_correction(correction)
        return copy.deepcopy(correction)

    # ------------------------------------------------------------------ 更正

    def correct(
        self,
        fact_id: str,
        changes: dict,
        reason: str,
        author: str,
        idempotency_key: str | None = None,
        version_id: str | None = None,
    ) -> dict:
        """在冻结版上追加一条更正（仅 ``amend`` 类型记录）。"""

        reason, author, key = _require_attribution(reason, author, idempotency_key)
        changes = _validate_changes(changes)
        with self._lock:
            self._block_while_draft()
            duplicate = self._find_duplicate(key, "amend", version_id, fact_id, changes, reason)
            if duplicate is not None:
                return {"deduplicated": True, "correction": copy.deepcopy(duplicate)}
            base = self._require_version(version_id) if version_id else self._require_latest()
            fact = self._require_fact(base, fact_id)
            require_kind(fact["record_type"], "amend")

            current = self._effective_fact(base["version_id"], fact_id)
            field_changes = {}
            for field, new_value in changes.items():
                field_changes[field] = {"old": current["data"].get(field), "new": new_value}
            correction = {
                "correction_id": f"cor-{uuid.uuid4().hex[:12]}",
                "idempotency_key": key or "",
                "kind": "amend",
                "shift_id": base["shift_id"],
                "version_id": base["version_id"],
                "new_version_id": None,
                "fact_id": fact_id,
                "record_type": fact["record_type"],
                "field_changes": field_changes,
                "reason": reason,
                "author": author,
                "ts": round(self._clock.now(), 3),
            }
            self._append_correction(correction)
            self._publish_correction(correction)
            return {"deduplicated": False, "correction": copy.deepcopy(correction)}

    def reopen(
        self,
        fact_id: str,
        changes: dict,
        reason: str,
        author: str,
        idempotency_key: str | None = None,
        version_id: str | None = None,
    ) -> dict:
        """对 ``reopen`` 类型记录重开一版：先开携带更正指令的子版本草稿。"""

        reason, author, key = _require_attribution(reason, author, idempotency_key)
        changes = _validate_changes(changes)
        with self._lock:
            if self._active is not None:
                # 重开请求在子版本冻结前重试：以草稿里携带的指令为幂等锚点。
                source = self._active.get("source")
                if (
                    self._active["draft_kind"] == "reopen"
                    and key
                    and source
                    and source.get("idempotency_key") == key
                ):
                    return {"deduplicated": True, "draft": self._draft_view()}
                duplicate = self._find_duplicate(key, "reopen", None, fact_id, changes, reason) if key else None
                if duplicate is not None:
                    return {"deduplicated": True, "correction": copy.deepcopy(duplicate)}
                raise InterlockError(
                    f"已有活动草稿 {self._active['shift_id']}（{self._active['draft_kind']}）未冻结，不能重开",
                    component="shift",
                )
            duplicate = self._find_duplicate(key, "reopen", version_id, fact_id, changes, reason) if key else None
            if duplicate is not None:
                return {"deduplicated": True, "correction": copy.deepcopy(duplicate)}

            base = self._require_version(version_id) if version_id else self._require_latest()
            fact = self._require_fact(base, fact_id)
            require_kind(fact["record_type"], "reopen")

            # 子版本以父版本"当前有效"的事实为底（含此前追加更正），否则旧更正
            # 会在新版本里被悄悄回退；目标事实再叠加本次重开的改动。旧更正的
            # 字段级标注随事实一起带过来，并标明是从上一版携带的。
            parent_view = self._effective_view(base)
            seed_facts: list[dict] = []
            for item in parent_view["facts"]:
                carried = [
                    {**change, "carried_from_version_id": base["version_id"]}
                    for change in item.get("changes", ())
                ]
                clean = {key: value for key, value in item.items() if key != "changes"}
                if carried:
                    clean["changes"] = carried
                seed_facts.append(clean)
            draft = {
                "shift_id": base["shift_id"],
                "seq": int(base["seq"]) + 1,
                "parent_version_id": base["version_id"],
                "draft_kind": "reopen",
                "started_ts": round(self._clock.now(), 3),
                "station": base["station"],
                "facts": seed_facts,
                "source": {
                    "base_version_id": base["version_id"],
                    "fact_id": fact_id,
                    "record_type": fact["record_type"],
                    "changes": dict(changes),
                    "reason": reason,
                    "author": author,
                    "idempotency_key": key or "",
                    "opened_ts": round(self._clock.now(), 3),
                },
            }
            seed = self._require_fact_raw(draft["facts"], fact_id)
            seed["data"].update(changes)
            self._active = draft
            self._persist_draft()
            self._publish(
                "shift_reopened",
                station=base["station"],
                shift_id=base["shift_id"],
                base_version_id=base["version_id"],
                fact_id=fact_id,
                record_type=fact["record_type"],
                fields=sorted(changes),
                idempotency_key=key or "",
                reason=reason,
                author=author,
                severity="warning",
                message=f"班次 {base['shift_id']} 因更正重开新版本",
            )
            return {"deduplicated": False, "draft": self._draft_view()}

    # ---------------------------------------------------------- 对外取数视图

    def handover(self) -> dict:
        """对外交接取数：只给最新冻结版叠加更正后的有效视图，绝不返回草稿。"""

        with self._lock:
            latest = self._latest_version_id()
            if latest is None:
                raise InterlockError("还没有冻结的交接版本，草稿不对外提供", component="shift")
            return self._effective_view(self._frozen[latest])

    def effective(self, version_id: str) -> dict:
        """任一冻结版的有效视图（叠加指向该版本的更正）。"""

        with self._lock:
            return self._effective_view(self._require_version(version_id))

    def _effective_view(self, base: dict) -> dict:
        facts = copy.deepcopy(base["facts"])
        by_id = {fact["fact_id"]: fact for fact in facts}
        applied: list[dict] = []
        for correction in self._corrections:
            target: dict | None = None
            if correction["kind"] == "amend" and correction["version_id"] == base["version_id"]:
                target = by_id.get(correction["fact_id"])
            elif correction["kind"] == "reopen" and correction.get("new_version_id") == base["version_id"]:
                target = by_id.get(correction["fact_id"])
            if target is None:
                continue
            for field, pair in correction["field_changes"].items():
                if correction["kind"] == "amend":
                    target["data"][field] = pair["new"]
                target.setdefault("changes", []).append(
                    {
                        "field": field,
                        "old": pair["old"],
                        "new": pair["new"],
                        "kind": correction["kind"],
                        "correction_id": correction["correction_id"],
                        "author": correction["author"],
                        "reason": correction["reason"],
                        "ts": correction["ts"],
                    }
                )
            applied.append(copy.deepcopy(correction))

        child = self._child_of(base["version_id"])
        return {
            "header": self._version_header(base),
            "facts": facts,
            "applied_corrections": applied,
            "superseded_by_version_id": child,
        }

    def _effective_fact(self, version_id: str, fact_id: str) -> dict:
        fact = copy.deepcopy(self._require_fact(self._frozen[version_id], fact_id))
        for correction in self._corrections:
            if correction["kind"] != "amend" or correction["version_id"] != version_id:
                continue
            if correction["fact_id"] != fact_id:
                continue
            for field, pair in correction["field_changes"].items():
                fact["data"][field] = pair["new"]
        return fact

    # ------------------------------------------------------------------ 对账

    def reconcile(self, audit_entries: Iterable[dict] | None = None) -> dict:
        """冻结件 × 更正流水 × 审计流水三处一致性核对。"""

        with self._lock:
            checks: list[dict] = []

            for body in self._frozen.values():
                # 冻结件的原件在磁盘上：先确认磁盘副本仍与签署时一致，再验指纹
                # 与签名。直接改磁盘文件（绕过服务）也能在这里被发现。
                on_disk = self._store.read(body["version_id"])
                disk_ok = isinstance(on_disk, dict) and content_equal(on_disk, body)
                checks.append(
                    {
                        "scope": "frozen",
                        "ref": body["version_id"],
                        "name": "disk_copy_matches",
                        "ok": disk_ok,
                    }
                )
                verdict = model.verify_frozen(body, self._key)
                checks.append({"scope": "frozen", "ref": body["version_id"], "name": "fingerprint_and_signatures", **verdict})
                parent_id = body.get("parent_version_id")
                if body["seq"] == 1:
                    parent_ok = parent_id is None
                else:
                    parent = self._frozen.get(str(parent_id or ""))
                    parent_ok = (
                        parent is not None
                        and parent["shift_id"] == body["shift_id"]
                        and parent["seq"] == body["seq"] - 1
                    )
                checks.append(
                    {
                        "scope": "frozen",
                        "ref": body["version_id"],
                        "name": "parent_chain",
                        "ok": parent_ok,
                    }
                )
                checks.append(
                    {
                        "scope": "frozen",
                        "ref": body["version_id"],
                        "name": "freeze_point_on_sign",
                        "ok": body.get("freeze_point") == FREEZE_POINT,
                    }
                )

            seen_keys: dict[str, str] = {}
            for correction in self._corrections:
                key = correction.get("idempotency_key") or ""
                unique = True
                if key:
                    unique = key not in seen_keys
                    seen_keys.setdefault(key, correction["correction_id"])
                checks.append(
                    {
                        "scope": "corrections",
                        "ref": correction["correction_id"],
                        "name": "idempotency_key_unique",
                        "ok": (not key) or unique,
                    }
                )
                checks.append(
                    {
                        "scope": "corrections",
                        "ref": correction["correction_id"],
                        "name": "targets_and_values",
                        "ok": self._check_correction_values(correction),
                    }
                )

            if audit_entries is not None:
                checks.extend(self._check_audit(list(audit_entries)))

            ok = all(item["ok"] for item in checks)
            return {
                "ok": ok,
                "frozen_versions": len(self._frozen),
                "corrections": len(self._corrections),
                "checks": checks,
                "failures": [item for item in checks if not item["ok"]],
            }

    def _check_correction_values(self, correction: dict) -> bool:
        if correction["kind"] == "amend":
            base = self._frozen.get(correction["version_id"])
            if base is None:
                return False
            fact = next((item for item in base["facts"] if item["fact_id"] == correction["fact_id"]), None)
            if fact is None or fact["record_type"] != correction["record_type"]:
                return False
            values = copy.deepcopy(fact["data"])
            # 按流水顺序逐条回放同版本同事实的 amend（不含本条），old 必须等于
            # "上一条更正后的现值"。
            for earlier in self._corrections:
                if earlier["correction_id"] == correction["correction_id"]:
                    break
                if (
                    earlier["kind"] == "amend"
                    and earlier["version_id"] == base["version_id"]
                    and earlier["fact_id"] == fact["fact_id"]
                ):
                    for field, pair in earlier["field_changes"].items():
                        values[field] = pair["new"]
            for field, pair in correction["field_changes"].items():
                if values.get(field) != pair["old"]:
                    return False
                values[field] = pair["new"]
            return True

        old = self._frozen.get(correction["version_id"])
        new = self._frozen.get(str(correction.get("new_version_id") or ""))
        if old is None or new is None:
            return False
        if new["parent_version_id"] != old["version_id"] or new["seq"] != old["seq"] + 1:
            return False
        # 父版本侧以"当前有效值"为旧值（重开时已把此前追加更正带入子版本）。
        old_fact = self._effective_fact(old["version_id"], correction["fact_id"])
        new_fact = next((item for item in new["facts"] if item["fact_id"] == correction["fact_id"]), None)
        if new_fact is None:
            return False
        if old_fact["record_type"] != correction["record_type"]:
            return False
        return all(
            old_fact["data"].get(field) == pair["old"] and new_fact["data"].get(field) == pair["new"]
            for field, pair in correction["field_changes"].items()
        )

    def _check_audit(self, entries: list[dict]) -> list[dict]:
        # AuditRecord.as_dict 里 action/component 在顶层，业务字段在 payload。
        audit_events = [
            entry for entry in entries if entry.get("component") == "shift"
        ]

        def has_event(action: str, **match: object) -> bool:
            for entry in audit_events:
                if entry.get("action") != action:
                    continue
                payload = entry.get("payload", {})
                if all(payload.get(key) == value for key, value in match.items()):
                    return True
            return False

        checks: list[dict] = []
        for body in self._frozen.values():
            checks.append(
                {
                    "scope": "audit",
                    "ref": body["version_id"],
                    "name": "frozen_event_present",
                    "ok": has_event(
                        "shift_frozen",
                        version_id=body["version_id"],
                        fingerprint=body["fingerprint"],
                    ),
                }
            )
        for correction in self._corrections:
            checks.append(
                {
                    "scope": "audit",
                    "ref": correction["correction_id"],
                    "name": "correction_event_present",
                    "ok": has_event("shift_corrected", correction_id=correction["correction_id"]),
                }
            )
        return checks

    # ------------------------------------------------------------------ 内部

    def _append_correction(self, correction: dict) -> None:
        self._corrections_journal.append(correction)
        self._corrections.append(correction)
        key = correction.get("idempotency_key")
        if key:
            self._idem[key] = correction

    def _publish_correction(self, correction: dict) -> None:
        self._publish(
            "shift_corrected",
            station=self._frozen.get(correction["version_id"], {}).get("station", ""),
            correction_id=correction["correction_id"],
            idempotency_key=correction.get("idempotency_key", ""),
            kind=correction["kind"],
            shift_id=correction["shift_id"],
            version_id=correction["version_id"],
            new_version_id=correction.get("new_version_id") or "",
            fact_id=correction["fact_id"],
            record_type=correction["record_type"],
            fields=sorted(correction["field_changes"]),
            reason=correction["reason"],
            author=correction["author"],
            severity="warning",
            message=f"{correction['kind']} 更正 {correction['fact_id']}",
        )

    def _publish(self, action: str, **payload: object) -> None:
        self._bus.publish(Event.of("shift", "shift", action, **payload))

    def _index_frozen(self, frozen: dict) -> None:
        version_id = frozen["version_id"]
        self._frozen[version_id] = frozen
        self._by_shift.setdefault(frozen["shift_id"], []).append(version_id)

    def _version_header(self, body: dict) -> dict:
        return {
            "version_id": body["version_id"],
            "shift_id": body["shift_id"],
            "seq": body["seq"],
            "parent_version_id": body.get("parent_version_id"),
            "freeze_point": body["freeze_point"],
            "frozen_ts": body["frozen_ts"],
            "started_ts": body["started_ts"],
            "draft_kind": body["draft_kind"],
            "station": body["station"],
            "fact_count": len(body["facts"]),
            "fingerprint": body["fingerprint"],
            "signers": [item["signer"] for item in body.get("signatures", ())],
        }

    def _latest_version_id(self) -> str | None:
        leaves = [vid for vid in self._frozen if self._child_of(vid) is None]
        if not leaves:
            return None
        return max(leaves, key=lambda vid: (self._frozen[vid]["frozen_ts"], vid))

    def _child_of(self, version_id: str) -> str | None:
        for body in self._frozen.values():
            if body.get("parent_version_id") == version_id:
                return body["version_id"]
        return None

    def _block_while_draft(self) -> None:
        if self._active is not None:
            raise InterlockError(
                f"活动草稿 {self._active['shift_id']} 尚未冻结或重签，不能追加更正",
                component="shift",
            )

    def _require_version(self, version_id: str | None) -> dict:
        if version_id and version_id in self._frozen:
            return self._frozen[version_id]
        raise InterlockError(f"冻结版 {version_id!r} 不存在", component="shift")

    def _require_latest(self) -> dict:
        latest = self._latest_version_id()
        if latest is None:
            raise InterlockError("还没有冻结的交接版本", component="shift")
        return self._frozen[latest]

    def _require_fact(self, version: dict, fact_id: str) -> dict:
        return self._require_fact_raw(version["facts"], fact_id)

    def _require_fact_raw(self, facts: list[dict], fact_id: str) -> dict:
        fact = next((item for item in facts if item["fact_id"] == fact_id), None)
        if fact is None:
            raise InterlockError(f"版本里找不到事实 {fact_id!r}", component="shift")
        return fact

    def _require_known_type(self, record_type: str) -> str:
        if record_type not in KNOWN_TYPES:
            raise InterlockError(
                f"未知记录类型 {record_type!r}，允许的类型：{sorted(KNOWN_TYPES)}",
                component="shift",
            )
        return record_type

    def _find_duplicate(
        self,
        key: str | None,
        kind: str,
        version_id: str | None,
        fact_id: str,
        changes: dict,
        reason: str,
    ) -> dict | None:
        if not key:
            return None
        existing = self._idem.get(key)
        if existing is None:
            return None
        same = (
            existing["kind"] == kind
            and existing["fact_id"] == fact_id
            and (version_id is None or existing["version_id"] == version_id)
            and existing.get("reason") == reason
            and {field: pair["new"] for field, pair in existing["field_changes"].items()} == dict(changes)
        )
        if not same:
            raise InterlockError(f"幂等键 {key!r} 已用于另一条更正，拒绝复用", component="shift")
        return existing


def _is_primitive(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def content_equal(left: object, right: object) -> bool:
    """两份内容是否逐字节等价（按稳定序列化比较）。"""

    return hmac.compare_digest(model.canonical_json(left), model.canonical_json(right))


def _clean_signers(signers: list[str]) -> list[str]:
    names = [str(name).strip() for name in signers if str(name).strip()]
    if not names:
        raise InterlockError("签署冻结至少需要一名签署人", component="shift")
    if len(set(names)) != len(names):
        raise InterlockError("同一签署人不能在一份交接件上重复签名", component="shift")
    return names


def _require_attribution(reason: str, author: str, idempotency_key: str | None) -> tuple[str, str, str | None]:
    reason = str(reason or "").strip()
    author = str(author or "").strip()
    if not reason:
        raise InterlockError("更正必须写明理由 reason", component="shift")
    if not author:
        raise InterlockError("更正必须写明更正人 author", component="shift")
    key = str(idempotency_key).strip() if idempotency_key else None
    return reason, author, key


def _validate_changes(changes: dict) -> dict:
    if not isinstance(changes, dict) or not changes:
        raise InterlockError("更正必须携带至少一个字段变更 changes", component="shift")
    cleaned: dict = {}
    for field, value in changes.items():
        field = str(field)
        if not field or not _is_primitive(value):
            raise InterlockError(f"更正字段 {field!r} 的值必须是标量", component="shift")
        cleaned[field] = value
    return cleaned
