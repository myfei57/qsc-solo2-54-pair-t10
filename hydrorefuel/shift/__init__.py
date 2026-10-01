"""班次交接签署。

把"抄一遍没人签"的交接变成一次签署：班次事实先在活动草稿里累积，签署时冻结
成带指纹与逐人签名的版本（冻结点定死为签署时刻）。冻结版历史不可变；之后
发现错误，按记录类型决定追加更正（人工录入类）还是重开一版（设备计量类），
更正流水与审计流水互相对账。对外取数只看最新冻结版叠加更正后的有效视图。
"""

from __future__ import annotations

from .book import ShiftBook
from .model import content_fingerprint, verify_frozen
from .policy import FREEZE_POINT, policy_for

__all__ = ["ShiftBook", "content_fingerprint", "verify_frozen", "FREEZE_POINT", "policy_for"]
