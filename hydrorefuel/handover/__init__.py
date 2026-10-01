"""班次交接：签署冻结、追加更正与一致性核对。

交接不再是"自己抄一遍、抄完没人签"。班次末先封口（停止再记充装事实），
签署动作把这一班的充装事实冻成带签名、带内容摘要的不可变版本；之后发现
错误只能按记录类型追加更正（计量记录）或重开一版（结论性记录），更正必须
写明理由、带更正人签名并携带幂等键。冻结、更正、审计三处可通过
:meth:`HandoverLedger.reconcile` 重算核对，对外取数永远只见冻结版。

冻结时点定死为签署时（:data:`~hydrorefuel.handover.policy.FREEZE_POINT`）。
"""

from __future__ import annotations

from .ledger import HandoverLedger
from .policy import (
    AMEND,
    BUFFER_CHARGE,
    CORRECTION_MODE,
    FILL_TRANSACTION,
    FREEZE_POINT,
    REOPEN,
    SHIFT_ATTESTATION,
    UNLOAD_RECEIPT,
    correction_mode,
    is_correctable,
)
from .signing import digest_content, sign_content, verify_signature

__all__ = [
    "HandoverLedger",
    "AMEND",
    "REOPEN",
    "FILL_TRANSACTION",
    "UNLOAD_RECEIPT",
    "BUFFER_CHARGE",
    "SHIFT_ATTESTATION",
    "CORRECTION_MODE",
    "FREEZE_POINT",
    "correction_mode",
    "is_correctable",
    "digest_content",
    "sign_content",
    "verify_signature",
]
