"""班次交接的记录类型与更正策略。

一份班次交接由若干"充装事实"组成。不同事实的法律效力不同，发现错误时的
处理方式也不同，这里把口径写死成策略表，调用方不能自选：

- 操作性计量记录（加注、卸氢、充瓶）错了只做**追加更正**：冻结版原样
  保留，更正以"原值 → 新值 + 理由 + 签名"的形式叠加在当前冻结版之上；
- 班次结论性记录（交接确认）错了必须**重开一版**：在原冻结版基础上
  生成待签署的新版本，重新签署后成为新冻结版，旧版原样留档。

冻结时点同样定死：**签署时冻结**（``FREEZE_POINT = "on_signing"``）。
班次末只"封口"，签署动作才产生不可变版本。
"""

from __future__ import annotations

# 冻结时点口径。整站只允许这一种取值，核对时直接比对该常量。
FREEZE_POINT = "on_signing"

# 更正方式
AMEND = "amend"
REOPEN = "reopen"

# 操作性计量记录：追加更正
FILL_TRANSACTION = "fill_transaction"
UNLOAD_RECEIPT = "unload_receipt"
BUFFER_CHARGE = "buffer_charge"

# 班次结论性记录：重开一版
SHIFT_ATTESTATION = "shift_attestation"

RECORD_TYPES = (FILL_TRANSACTION, UNLOAD_RECEIPT, BUFFER_CHARGE, SHIFT_ATTESTATION)

# 记录类型 → 更正方式
CORRECTION_MODE = {
    FILL_TRANSACTION: AMEND,
    UNLOAD_RECEIPT: AMEND,
    BUFFER_CHARGE: AMEND,
    SHIFT_ATTESTATION: REOPEN,
}

# 各类型允许更正的字段。计量记录只允许改计量值，不允许改归属字段
# （车号、枪号这类归属错误属于结论错误，应登记为 shift_attestation 重开）。
CORRECTABLE_FIELDS = {
    FILL_TRANSACTION: ("mass_kg", "rate_kg_per_min", "steps"),
    UNLOAD_RECEIPT: ("delivered_kg",),
    BUFFER_CHARGE: ("charge_kg", "pressure_mpa"),
    SHIFT_ATTESTATION: ("result", "remark"),
}


def correction_mode(record_type: str) -> str:
    """返回某记录类型的更正方式，未知类型直接拒绝。"""

    mode = CORRECTION_MODE.get(record_type)
    if mode is None:
        raise ValueError(f"未知记录类型: {record_type!r}")
    return mode


def is_correctable(record_type: str, field: str) -> bool:
    """某类型的某字段是否允许被更正。"""

    return field in CORRECTABLE_FIELDS.get(record_type, ())
