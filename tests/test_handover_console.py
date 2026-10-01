"""班次交接的控制台 HTTP 接口端到端。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from hydrorefuel.console.server import ConsoleServer
from hydrorefuel.handover import FILL_TRANSACTION


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_handover_sign_freeze_correct_reopen_and_reconcile(platform):
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        assert _post(f"{base}/api/handover/open", {"shift_id": "S"})[0] == 200
        ok, _ = _post(
            f"{base}/api/handover/fact",
            {"shift_id": "S", "record_type": FILL_TRANSACTION, "values": {"vehicle": "v1", "mass_kg": 10.0}},
        )
        assert ok == 200

        # 签署前：对外取数被拒，控制台能看到草稿。
        assert _get(f"{base}/api/handover/current?shift_id=S")[0] == 409
        draft = _get(f"{base}/api/handover/draft?shift_id=S")[1]
        assert draft["facts"][0]["values"]["mass_kg"] == 10.0

        # 未封口直接签署被联锁拒绝（必须先封口再签署）。
        assert _post(f"{base}/api/handover/sign", {"shift_id": "S", "signer": "张三"})[0] == 409
        assert _post(f"{base}/api/handover/seal", {"shift_id": "S"})[0] == 200
        status, payload = _post(f"{base}/api/handover/sign", {"shift_id": "S", "signer": "张三"})
        assert status == 200
        version = payload["version"]
        assert version["version"] == "v1"
        assert version["freeze_point"] == "on_signing"
        fact_id = version["records"][0]["fact_id"]

        # 冻结后改草稿事实被拒（封口后不能再记）。
        blocked, _ = _post(
            f"{base}/api/handover/fact",
            {"shift_id": "S", "record_type": FILL_TRANSACTION, "values": {"mass_kg": 99.0}},
        )
        assert blocked == 409

        # 更正必须给理由、签名人和幂等键。
        assert _post(f"{base}/api/handover/correct", {"shift_id": "S", "fact_id": fact_id, "field": "mass_kg", "new_value": 12.0})[0] == 409

        correction_body = {
            "shift_id": "S",
            "fact_id": fact_id,
            "field": "mass_kg",
            "new_value": 12.0,
            "reason": "流量计复核少计",
            "author": "李四",
            "idem_key": "corr-1",
        }
        first_status, first = _post(f"{base}/api/handover/correct", correction_body)
        assert first_status == 200
        # 同一请求重放：幂等成功且是同一条更正。
        retry_status, retry = _post(f"{base}/api/handover/correct", correction_body)
        assert retry_status == 200
        assert retry["correction"]["idempotent"] is True
        assert retry["correction"]["correction_id"] == first["correction"]["correction_id"]

        # 对外取数看到更正后的值，且改痕、签署人可见；历史 v1 原样。
        current = _get(f"{base}/api/handover/current?shift_id=S")[1]["version"]
        assert current["records"][0]["values"]["mass_kg"] == 12.0
        assert current["records"][0]["changes"][0]["author"] == "李四"
        historical = _get(f"{base}/api/handover/version?shift_id=S&version=v1")[1]["version"]
        assert historical["records"][0]["values"]["mass_kg"] == 10.0

        # 冻结、更正、审计三处核对一致。
        reconcile = _get(f"{base}/api/handover/reconcile?shift_id=S")[1]
        assert reconcile["ok"] is True, reconcile["problems"]

        # 冻结版本在版本清单里可查（控制台展示）。
        versions = _get(f"{base}/api/handover/versions?shift_id=S")[1]["versions"]
        assert [item["version"] for item in versions] == ["v1"]
    finally:
        server.stop()


def test_handover_external_current_never_serves_draft_for_new_shift(platform):
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        # 冻好一班
        _post(f"{base}/api/handover/open", {"shift_id": "S1"})
        _post(f"{base}/api/handover/fact", {"shift_id": "S1", "record_type": FILL_TRANSACTION, "values": {"mass_kg": 1.0}})
        _post(f"{base}/api/handover/seal", {"shift_id": "S1"})
        _post(f"{base}/api/handover/sign", {"shift_id": "S1", "signer": "甲"})
        assert _get(f"{base}/api/handover/current?shift_id=S1")[1]["version"]["version"] == "v1"
        # 下一班只封口未签署：默认取数应落到新班但被拒，而不是偷看上一班。
        _post(f"{base}/api/handover/open", {"shift_id": "S2"})
        _post(f"{base}/api/handover/fact", {"shift_id": "S2", "record_type": FILL_TRANSACTION, "values": {"mass_kg": 2.0}})
        _post(f"{base}/api/handover/seal", {"shift_id": "S2"})
        assert _get(f"{base}/api/handover/current")[0] == 409
    finally:
        server.stop()
