"""领域枚举与命令对象。

核心概念：
- Equipment / Component：设备与部件实例，部件有唯一序列号，替换后拓扑随之更新。
- WorkOrderStage：工单阶段，同一故障在不同阶段开放不同处置权限。
- Role：处置角色，权限 = 角色 × 阶段 × 动作。
- FaultSeverity / PartStatus / DiagnosisVerdict / ContractCoverage：业务枚举。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    DISPATCH = "dispatch"          # 客服：受理、派单、关闭（确认）
    DIAGNOSTIC = "diagnostic"      # 诊断工程师：读数分析、结论
    PARTS = "parts"                # 备件团队：确认预留、缺货登记、到货
    REPAIR = "repair"              # 现场维修：到场、换件、修复
    SUPERVISOR = "supervisor"      # 主管：跨团队转交、合同调整、强制流转


class WorkOrderStage(str, Enum):
    OPEN = "open"                  # 已受理，待诊断
    DIAGNOSED = "diagnosed"        # 已出诊断结论，待维修/备件
    WAITING_PARTS = "waiting_parts"  # 已确认需要备件，等待中
    IN_REPAIR = "in_repair"        # 维修现场处置中
    RESOLVED = "resolved"          # 现场处置完成，待客服核验关闭
    CLOSED = "closed"              # 终态
    CANCELED = "canceled"          # 终态（误报/客户取消）


# 阶段状态机：允许的普通流转。转交不改变阶段。
STAGE_TRANSITIONS: dict[WorkOrderStage, frozenset[WorkOrderStage]] = {
    WorkOrderStage.OPEN: frozenset({WorkOrderStage.DIAGNOSED, WorkOrderStage.CANCELED}),
    WorkOrderStage.DIAGNOSED: frozenset({
        WorkOrderStage.WAITING_PARTS, WorkOrderStage.IN_REPAIR, WorkOrderStage.CANCELED,
    }),
    WorkOrderStage.WAITING_PARTS: frozenset({
        WorkOrderStage.IN_REPAIR, WorkOrderStage.CANCELED,
    }),
    WorkOrderStage.IN_REPAIR: frozenset({WorkOrderStage.RESOLVED, WorkOrderStage.CANCELED}),
    WorkOrderStage.RESOLVED: frozenset({WorkOrderStage.CLOSED}),
    WorkOrderStage.CLOSED: frozenset(),
    WorkOrderStage.CANCELED: frozenset(),
}

# 主管可退回（返工）的额外流转；普通角色不可用。
SUPERVISOR_ROLLBACK: dict[WorkOrderStage, frozenset[WorkOrderStage]] = {
    WorkOrderStage.DIAGNOSED: frozenset({WorkOrderStage.OPEN}),
    WorkOrderStage.IN_REPAIR: frozenset({WorkOrderStage.DIAGNOSED}),
    WorkOrderStage.RESOLVED: frozenset({WorkOrderStage.IN_REPAIR}),
}


class FaultSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DiagnosisVerdict(str, Enum):
    CONFIRMED = "confirmed"        # 故障确认，需处置
    FALSE_ALARM = "false_alarm"    # 误报
    OBSERVE = "observe"            # 带病观察，暂不处置
    REPAIR_SCOPE = "repair_scope"  # 明确维修范围与所需备件


class PartRequestStatus(str, Enum):
    REQUESTED = "requested"        # 已申请，待备件团队确认
    RESERVED = "reserved"          # 已预留
    BACKORDERED = "backordered"    # 缺货，已下补订（记录预计到货）
    FULFILLED = "fulfilled"        # 已出库用于维修
    CANCELED = "canceled"          # 取消（工单取消或方案变更）


class ContractCoverage(str, Enum):
    WARRANTY = "warranty"          # 保修：免费
    SERVICE_CONTRACT = "service_contract"  # 服务合同：按合同条款
    OUT_OF_WARRANTY = "out_of_warranty"    # 脱保：有偿
    GOODWILL = "goodwill"          # 善意保修（特批）


# ---------------------------------------------------------------------------
# 命令对象：服务 API 的输入。全部为纯数据，校验在 aggregate/service 层完成。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisterEquipment:
    serial: str
    model: str
    customer: str
    delivered_at: str                       # ISO8601，现场交付时间
    operator: str
    bill_of_materials: dict[str, str] = field(default_factory=dict)  # slot -> 部件序列号
    warranty_months: int = 12
    occurred_at: str | None = None          # 补录时显式指定业务时间


@dataclass(frozen=True)
class InstallEquipment:
    serial: str
    site: str
    operator: str
    occurred_at: str | None = None


@dataclass(frozen=True)
class ReplacePart:
    equipment_serial: str
    slot: str
    old_part_serial: str | None             # None 表示该槽位此前为空
    new_part_serial: str
    operator: str
    work_order_id: str | None = None        # 关联工单（计划性更换可为空）
    occurred_at: str | None = None
    backfill: bool = False                  # 现场离线、事后补录
    note: str = ""


@dataclass(frozen=True)
class ChangeWarranty:
    """保修范围变化：延期/缩水/转为合同/特批，保留变化原因与审批人。"""

    equipment_serial: str
    new_coverage: str
    operator: str
    reason: str
    warranty_end: str | None = None         # 新的保修截止日（如有）
    occurred_at: str | None = None


@dataclass(frozen=True)
class IngestReading:
    equipment_serial: str
    sensor: str
    observed_at: str                        # 采集时刻（现场）
    values: dict[str, float]
    source_uri: str = ""                    # 原始数据落点（对象存储/时序库 URI）
    recorded_at: str | None = None          # 入库时刻（离线补录时可能晚于 observed_at）
    backfill: bool = False


@dataclass(frozen=True)
class ReportFault:
    equipment_serial: str
    fault_code: str
    title: str
    reporter: str
    role: str = Role.DISPATCH.value
    severity: str = FaultSeverity.MEDIUM.value
    occurred_at: str | None = None
    reading_ids: tuple[str, ...] = ()
    description: str = ""
    backfill: bool = False                  # 离线维修补录
    idempotency_key: str | None = None      # 客户端去重键（重复工单/重试）


@dataclass(frozen=True)
class WorkOrderAction:
    """工单上的一次处置动作（含诊断、派单、备件、换件、修复、关闭等）。"""

    work_order_id: str
    action: str
    operator: str
    role: str
    occurred_at: str | None = None
    backfill: bool = False
    # 各动作按需取用的字段
    to_team: str | None = None              # 跨团队转交目标
    verdict: str | None = None              # 诊断结论
    root_cause: str = ""
    required_parts: tuple[tuple[str, str], ...] = ()  # (part_serial, slot)
    reading_ids: tuple[str, ...] = ()
    part_serial: str | None = None
    eta: str | None = None                  # 缺货预计到货
    resolution: str = ""
    note: str = ""
    target_stage: str | None = None         # 主管强制流转
    coverage: str | None = None             # 费用归属确认：warranty/contract/chargeable
    idempotency_key: str | None = None      # 客户端去重键（动作重试）


# 动作常量（WorkOrderAction.action 取值）
class Action:
    DIAGNOSE = "diagnose"
    TRANSFER = "transfer"
    REQUEST_PARTS = "request_parts"
    CANCEL_PART = "cancel_part"
    RESERVE_PART = "reserve_part"
    REPORT_SHORTAGE = "report_shortage"
    PART_ARRIVED = "part_arrived"
    DISPATCH_PART = "dispatch_part"
    START_REPAIR = "start_repair"
    RESOLVE = "resolve"
    CLOSE = "close"
    CANCEL = "cancel"
    FORCE_STAGE = "force_stage"
