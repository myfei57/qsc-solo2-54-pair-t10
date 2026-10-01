"""班次交接的记录类型策略。

一份交接件里能出现的记录只有两类处置方式，由记录类型（``record_type``）唯
一决定，不允许调用方现场选择：

``amend``（追加更正）
    人工录入或汇总性质的记录，冻结后发现错误时在原版本上**追加**一条更正，
    原版本不动，有效视图叠加更正后的新值。

``reopen``（重开一版）
    设备计量的原始事实（卸氢、加注），冻结后只能在同一班次下**重开**一个子
    版本改正，改正经重新签署后成为新的冻结版；旧版本永久保留为历史。

冻结时机同样在这里定死：只在**签署时**冻结（``on_sign``），不存在"班次末
自动冻结"的路径。常量写进每一份冻结件，核对时可以直接看到口径。
"""

from __future__ import annotations

from ..errors import InterlockError

# 冻结时机定死：签署即冻结，签署那一刻的内容就是被冻内容。
FREEZE_POINT = "on_sign"

# 设备计量原始事实 → 必须重开一版。
REOPEN_TYPES = frozenset({"fill", "unload"})
# 人工录入／汇总／说明 → 追加更正。
AMEND_TYPES = frozenset({"meter_reading", "note"})

KNOWN_TYPES = REOPEN_TYPES | AMEND_TYPES

# 更正的两种形态，与记录类型策略一一对应。
CORRECTION_KINDS = ("amend", "reopen")


def policy_for(record_type: str) -> str:
    """返回某记录类型的更正策略：``amend`` 或 ``reopen``。"""

    kind = str(record_type)
    if kind in REOPEN_TYPES:
        return "reopen"
    if kind in AMEND_TYPES:
        return "amend"
    raise InterlockError(
        f"未知记录类型 {record_type!r}，允许的类型：{sorted(KNOWN_TYPES)}",
        component="shift",
    )


def require_kind(record_type: str, expected: str) -> str:
    """断言记录类型必须走 ``expected`` 形态，否则拒绝并给出另一种做法。"""

    actual = policy_for(record_type)
    if actual != expected:
        attempted = "追加更正" if expected == "amend" else "重开一版"
        raise InterlockError(
            f"记录类型 {record_type!r} 的策略是 {actual}，不能{attempted}；"
            f"请改用 {'/api/shift/reopen' if actual == 'reopen' else '/api/shift/correct'}",
            component="shift",
        )
    return actual
