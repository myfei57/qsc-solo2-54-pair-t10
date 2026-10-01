"""交接件的内容指纹与签名。

冻结版是"冻完不能再改"的凭据，因此：

* 被冻内容（版本号、父子关系、时刻、事实清单）先按稳定 JSON 序列化后算
  SHA-256，作为**内容指纹**；
* 每位签署人用站内签名密钥对指纹做 HMAC-SHA256，签名块随冻结件一起落盘；
* 复核时重算内容指纹、逐个验签，任何字节被改动都会让指纹或签名对不上。

密钥由站内生成、仅落盘在存储目录（文档 ``shift_signing_key``），属对称的"本
站签发、本站复核"模型；多站互信需要换发密钥，不在本子模块范围内。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

# 参与指纹计算的顶层字段（签名块本身不参与，否则自引用）。
FROZEN_BODY_FIELDS = (
    "version_id",
    "shift_id",
    "seq",
    "parent_version_id",
    "freeze_point",
    "frozen_ts",
    "started_ts",
    "draft_kind",
    "station",
    "facts",
)


def canonical_json(payload: object) -> bytes:
    """稳定序列化：键排序、无空白、非 ASCII 原样，保证字节可复算。"""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_fingerprint(body: dict) -> str:
    """计算一份冻结件的内容指纹（SHA-256 十六进制）。"""

    material = {key: body.get(key) for key in FROZEN_BODY_FIELDS}
    return hashlib.sha256(canonical_json(material)).hexdigest()


def sign_fingerprint(fingerprint: str, signer: str, key: bytes) -> str:
    """对内容指纹做 HMAC-SHA256，返回十六进制签名。"""

    return hmac.new(key, fingerprint.encode("utf-8"), hashlib.sha256).hexdigest()


def make_signatures(fingerprint: str, signers: list[str], key: bytes, signed_ts: float) -> list[dict]:
    """为每个签署人生成一块签名。"""

    return [
        {
            "signer": signer,
            "signed_ts": round(signed_ts, 3),
            "alg": "HMAC-SHA256",
            "digest": sign_fingerprint(fingerprint, signer, key),
        }
        for signer in signers
    ]


def verify_frozen(frozen: dict, key: bytes) -> dict:
    """复核一份冻结件：重算指纹并逐个验签，返回逐条结论。"""

    signatures = frozen.get("signatures", [])
    recomputed = content_fingerprint(frozen)
    stored = str(frozen.get("fingerprint", ""))
    checks = [
        {
            "signer": str(item.get("signer", "")),
            "ok": hmac.compare_digest(
                str(item.get("digest", "")),
                sign_fingerprint(recomputed, str(item.get("signer", "")), key),
            ),
        }
        for item in signatures
    ]
    return {
        "version_id": frozen.get("version_id"),
        "fingerprint_ok": hmac.compare_digest(recomputed, stored),
        "signatures": checks,
        "ok": hmac.compare_digest(recomputed, stored)
        and bool(checks)
        and all(item["ok"] for item in checks),
    }


def new_signing_key() -> str:
    """生成一支新的站内签名密钥（十六进制 32 字节）。"""

    return os.urandom(32).hex()
