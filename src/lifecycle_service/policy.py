"""处置权限矩阵与阶段流转规则。

同一故障在不同服务阶段开放不同处置权限；权限由 (角色, 阶段, 动作) 三元组决定，
工单归属团队形成第二道约束（转交后原团队不再持有处置权）。
"""

from __future__ import annotations

from .errors import InvalidStageError, PermissionDeniedError
from .models import (
    SUPERVISOR_ROLLBACK,
    STAGE_TRANSITIONS,
    Action,
    PartRequestStatus,
    Role,
    WorkOrderStage,
)

# 每个动作：允许的角色 + 允许发起的工单阶段
POLICY: dict[str, dict[str, frozenset[str]]] = {
    Action.DIAGNOSE: {
        "roles": frozenset({Role.DIAGNOSTIC.value, Role.SUPERVISOR.value}),
        "stages": frozenset({WorkOrderStage.OPEN.value}),
    },
    Action.TRANSFER: {
        "roles": frozenset({Role.SUPERVISOR.value, Role.DISPATCH.value}),
        "stages": frozenset({
            WorkOrderStage.OPEN.value, WorkOrderStage.DIAGNOSED.value,
            WorkOrderStage.WAITING_PARTS.value, WorkOrderStage.IN_REPAIR.value,
            WorkOrderStage.RESOLVED.value,
        }),
    },
    Action.REQUEST_PARTS: {
        "roles": frozenset({Role.DIAGNOSTIC.value, Role.REPAIR.value,
                            Role.SUPERVISOR.value}),
        "stages": frozenset({WorkOrderStage.DIAGNOSED.value}),
    },
    Action.CANCEL_PART: {
        "roles": frozenset({Role.PARTS.value, Role.REPAIR.value, Role.SUPERVISOR.value}),
        "stages": frozenset({WorkOrderStage.WAITING_PARTS.value,
                             WorkOrderStage.IN_REPAIR.value}),
    },
    Action.RESERVE_PART: {
        "roles": frozenset({Role.PARTS.value}),
        "stages": frozenset({WorkOrderStage.WAITING_PARTS.value}),
    },
    Action.REPORT_SHORTAGE: {
        "roles": frozenset({Role.PARTS.value}),
        "stages": frozenset({WorkOrderStage.WAITING_PARTS.value}),
    },
    Action.PART_ARRIVED: {
        "roles": frozenset({Role.PARTS.value}),
        "stages": frozenset({WorkOrderStage.WAITING_PARTS.value}),
    },
    Action.DISPATCH_PART: {
        "roles": frozenset({Role.PARTS.value}),
        "stages": frozenset({WorkOrderStage.WAITING_PARTS.value,
                             WorkOrderStage.IN_REPAIR.value}),
    },
    Action.START_REPAIR: {
        "roles": frozenset({Role.REPAIR.value, Role.SUPERVISOR.value}),
        "stages": frozenset({WorkOrderStage.DIAGNOSED.value,
                             WorkOrderStage.WAITING_PARTS.value}),
    },
    Action.RESOLVE: {
        "roles": frozenset({Role.REPAIR.value}),
        "stages": frozenset({WorkOrderStage.IN_REPAIR.value}),
    },
    Action.CLOSE: {
        "roles": frozenset({Role.DISPATCH.value}),
        "stages": frozenset({WorkOrderStage.RESOLVED.value}),
    },
    Action.CANCEL: {
        "roles": frozenset({Role.DISPATCH.value, Role.SUPERVISOR.value}),
        "stages": frozenset({
            WorkOrderStage.OPEN.value, WorkOrderStage.DIAGNOSED.value,
            WorkOrderStage.WAITING_PARTS.value, WorkOrderStage.IN_REPAIR.value,
            WorkOrderStage.RESOLVED.value,
        }),
    },
    Action.FORCE_STAGE: {
        "roles": frozenset({Role.SUPERVISOR.value}),
        "stages": frozenset({
            WorkOrderStage.OPEN.value, WorkOrderStage.DIAGNOSED.value,
            WorkOrderStage.WAITING_PARTS.value, WorkOrderStage.IN_REPAIR.value,
            WorkOrderStage.RESOLVED.value,
        }),
    },
}

# 转交允许的目标团队
KNOWN_TEAMS = frozenset({"dispatch", "diagnostic", "parts", "repair"})

# 各阶段的责任团队（强制流转/返工时据此同步归属）
STAGE_TEAM: dict[str, str] = {
    WorkOrderStage.OPEN.value: "dispatch",
    WorkOrderStage.DIAGNOSED.value: "diagnostic",
    WorkOrderStage.WAITING_PARTS.value: "parts",
    WorkOrderStage.IN_REPAIR.value: "repair",
    WorkOrderStage.RESOLVED.value: "dispatch",
}


def authorize(action: str, role: str, stage: str) -> None:
    """校验角色与阶段权限；不通过抛 PermissionDeniedError。"""
    rule = POLICY.get(action)
    if rule is None:
        raise InvalidStageError(f"未知动作：{action}")
    if role not in rule["roles"]:
        raise PermissionDeniedError(
            f"角色 {role} 无权在工单上执行 {action}")
    if stage not in rule["stages"]:
        raise PermissionDeniedError(
            f"工单处于 {stage} 阶段，不允许执行 {action}（同故障在不同阶段处置权限不同）")


def ensure_transition(current: str, target: str, supervisor: bool = False) -> None:
    cur = WorkOrderStage(current)
    tgt = WorkOrderStage(target)
    if tgt in STAGE_TRANSITIONS[cur]:
        return
    if supervisor and tgt in SUPERVISOR_ROLLBACK.get(cur, frozenset()):
        return
    raise InvalidStageError(f"不允许从 {current} 转为 {target}")


def parts_ready_for_repair(part_requests: dict) -> tuple[bool, list[str]]:
    """开始现场维修前，所有申请的备件必须已预留或已出库。"""
    pending = [
        serial for serial, req in part_requests.items()
        if req.status in (PartRequestStatus.REQUESTED.value,
                          PartRequestStatus.BACKORDERED.value)
    ]
    return not pending, pending
