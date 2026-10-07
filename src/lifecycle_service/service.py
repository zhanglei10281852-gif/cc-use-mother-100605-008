"""生命周期领域服务。

一个进程内 ``LifecycleService`` 持有事件日志与内存投影：
- 所有写操作在同一把锁内完成“校验 → 追加事件 → 更新投影”，原子且串行；
- 事件 fsync 落盘，重启重放即恢复，未关闭工单不会丢失；
- 读操作直接查投影，并可按任意历史时刻重建设备拓扑与保修结论。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import timedelta
from typing import Any

from .errors import (
    BackfillRejectedError,
    ConflictError,
    DuplicateWorkOrderError,
    NotFoundError,
    ValidationFailure,
)
from .events import Event, parse_iso, utc_now_iso
from .models import (
    Action,
    ChangeWarranty,
    ContractCoverage,
    DiagnosisVerdict,
    FaultSeverity,
    IngestReading,
    InstallEquipment,
    PartRequestStatus,
    RegisterEquipment,
    ReplacePart,
    ReportFault,
    Role,
    WorkOrderAction,
    WorkOrderStage,
)
from .policy import (
    KNOWN_TEAMS,
    authorize,
    ensure_transition,
    parts_ready_for_repair,
)
from .projection import Projection
from .store import EventLog

# 现场事件超过此时延才标记 backfill（在线系统轻微时钟偏差不触发）。
BACKFILL_GRACE = timedelta(hours=1)
READING_BACKFILL_GRACE = timedelta(hours=24)


class LifecycleService:
    def __init__(self, log: EventLog | str):
        self.log = log if isinstance(log, EventLog) else EventLog(log)
        self.projection = Projection()
        self._lock = threading.RLock()
        for event in self.log.read_all():
            self.projection.apply(event)

    # ------------------------------------------------------------------
    # 内部基础
    # ------------------------------------------------------------------

    def _append(self, event_type: str, aggregate_id: str, occurred_at: str | None,
                operator: str, payload: dict[str, Any], backfill: bool = False) -> Event:
        event = self.log.append(
            event_type=event_type,
            aggregate_id=aggregate_id,
            occurred_at=occurred_at or utc_now_iso(),
            operator=operator,
            payload=payload,
            backfill=backfill,
        )
        self.projection.apply(event)
        return event

    @staticmethod
    def _require_text(value: str | None, field: str) -> str:
        if value is None or not value.strip():
            raise ValidationFailure(f"{field}不能为空")
        return value.strip()

    def _equipment(self, serial: str):
        eq = self.projection.equipment_or_none(serial)
        if eq is None:
            raise NotFoundError(f"设备不存在：{serial}")
        return eq

    def _work_order(self, wo_id: str):
        wo = self.projection.work_order_or_none(wo_id)
        if wo is None:
            raise NotFoundError(f"工单不存在：{wo_id}")
        return wo

    def _idem(self, scope: tuple[str, str]) -> Event | None:
        event_id = self.projection.idem_keys.get(scope)
        if event_id is None:
            return None
        for event in self.projection.events_by_id.values():
            if event.event_id == event_id:
                return event
        return None

    def _check_backfill(self, occurred_at: str | None, flag: bool,
                        grace: timedelta = BACKFILL_GRACE) -> str:
        ts = occurred_at or utc_now_iso()
        lag = parse_iso(utc_now_iso()) - parse_iso(ts)
        if lag > grace and not flag:
            raise BackfillRejectedError(
                f"事件发生时间 {ts} 早于当前时间超过 {grace}，"
                "属于离线补录，必须显式 backfill=true")
        if flag and lag <= timedelta(0):
            raise ValidationFailure("backfill 事件的发生时间不能在未来")
        return ts

    def _ensure_timeline_order(self, wo, occurred_at: str) -> None:
        if wo.timeline:
            last = wo.timeline[-1]["occurred_at"]
            if parse_iso(occurred_at) < parse_iso(last):
                raise BackfillRejectedError(
                    f"补录时间 {occurred_at} 早于工单上已有动作 {last}；"
                    "请按现场发生顺序补录，历史链条不允许乱序插入")
        elif parse_iso(occurred_at) < parse_iso(wo.occurred_at):
            raise BackfillRejectedError(
                f"补录时间 {occurred_at} 早于报修时间 {wo.occurred_at}")

    # ------------------------------------------------------------------
    # 设备交付 / 安装
    # ------------------------------------------------------------------

    def register_equipment(self, cmd: RegisterEquipment) -> Event:
        serial = self._require_text(cmd.serial, "设备序列号")
        model = self._require_text(cmd.model, "型号")
        customer = self._require_text(cmd.customer, "客户")
        operator = self._require_text(cmd.operator, "操作人")
        delivered = parse_iso(cmd.occurred_at or cmd.delivered_at).isoformat()
        if cmd.warranty_months <= 0:
            raise ValidationFailure("保修月数必须为正")
        with self._lock:
            if self.projection.equipment_or_none(serial) is not None:
                raise ConflictError(f"设备序列号已存在：{serial}")
            for part in cmd.bill_of_materials.values():
                if part in self.projection.installed_parts:
                    raise ConflictError(f"部件 {part} 已装在其他设备上，序列号必须唯一")
            return self._append(
                "EquipmentRegistered", serial, delivered, operator,
                {"model": model, "customer": customer,
                 "bill_of_materials": dict(cmd.bill_of_materials),
                 "warranty_months": cmd.warranty_months},
            )

    def install_equipment(self, cmd: InstallEquipment) -> Event:
        serial = self._require_text(cmd.serial, "设备序列号")
        site = self._require_text(cmd.site, "安装地点")
        operator = self._require_text(cmd.operator, "操作人")
        with self._lock:
            eq = self._equipment(serial)
            if eq.installed_at is not None:
                raise ConflictError(f"设备已安装于 {eq.site}，不得重复安装")
            ts = parse_iso(cmd.occurred_at or utc_now_iso()).isoformat()
            if parse_iso(ts) < parse_iso(eq.delivered_at):
                raise ValidationFailure("安装时间不能早于设备交付时间")
            return self._append("EquipmentInstalled", serial, ts, operator, {"site": site})

    # ------------------------------------------------------------------
    # 部件替换（拓扑维护）
    # ------------------------------------------------------------------

    def replace_part(self, cmd: ReplacePart) -> Event:
        equipment_serial = self._require_text(cmd.equipment_serial, "设备序列号")
        slot = self._require_text(cmd.slot, "槽位")
        new_part = self._require_text(cmd.new_part_serial, "新部件序列号")
        operator = self._require_text(cmd.operator, "操作人")
        with self._lock:
            eq = self._equipment(equipment_serial)
            current = eq.bom.get(slot)
            if current is None and cmd.old_part_serial is not None:
                raise ConflictError(f"槽位 {slot} 当前为空，记录的旧部件 {cmd.old_part_serial} 不匹配")
            if current is not None and cmd.old_part_serial != current:
                raise ConflictError(
                    f"槽位 {slot} 当前部件为 {current}，与申报的旧部件 "
                    f"{cmd.old_part_serial} 不符，禁止记账")
            host = self.projection.installed_parts.get(new_part)
            if host is not None and host != (equipment_serial, slot):
                raise ConflictError(f"部件 {new_part} 已装在 {host[0]} 的 {host[1]}，不可重复安装")
            linked = None
            if cmd.work_order_id:
                linked = self._work_order(cmd.work_order_id)
                if linked.equipment_serial != equipment_serial:
                    raise ConflictError("换件工单不属于该设备")
                if linked.stage != WorkOrderStage.IN_REPAIR.value:
                    raise ConflictError(
                        f"工单处于 {linked.stage}，只有现场维修中才能登记换件")
                req = linked.parts.get(new_part)
                if req is not None and req.status != PartRequestStatus.FULFILLED.value:
                    raise ConflictError(
                        f"部件 {new_part} 在工单上状态为 {req.status}，必须先出库才能装入")
            ts = self._check_backfill(cmd.occurred_at or utc_now_iso(), cmd.backfill)
            if parse_iso(ts) < parse_iso(eq.delivered_at):
                raise ValidationFailure("换件时间不能早于设备交付时间")
            if linked is not None:
                # 关联工单的换件属于维修经过，必须符合现场发生顺序。
                self._ensure_timeline_order(linked, ts)
            return self._append(
                "PartReplaced", equipment_serial, ts, operator,
                {"slot": slot, "old_part_serial": cmd.old_part_serial,
                 "new_part_serial": new_part, "work_order_id": cmd.work_order_id,
                 "note": cmd.note},
            )

    # ------------------------------------------------------------------
    # 保修/合同范围变化
    # ------------------------------------------------------------------

    def change_warranty(self, cmd: ChangeWarranty) -> Event:
        serial = self._require_text(cmd.equipment_serial, "设备序列号")
        reason = self._require_text(cmd.reason, "变更原因")
        operator = self._require_text(cmd.operator, "操作人")
        valid = {c.value for c in ContractCoverage}
        if cmd.new_coverage not in valid:
            raise ValidationFailure(f"保修范围取值非法：{cmd.new_coverage}")
        warranty_end = None
        if cmd.warranty_end:
            warranty_end = parse_iso(cmd.warranty_end).isoformat()
        with self._lock:
            self._equipment(serial)
            # 合同/保修变更允许携带过去的生效日期（条款可追溯生效），
            # 系统如实标记 backfill，而不是拒绝。
            ts = parse_iso(cmd.occurred_at or utc_now_iso()).isoformat()
            backfill = parse_iso(utc_now_iso()) - parse_iso(ts) > BACKFILL_GRACE
            return self._append(
                "WarrantyChanged", serial, ts, operator,
                {"new_coverage": cmd.new_coverage, "reason": reason,
                 "warranty_end": warranty_end},
                backfill=backfill,
            )

    # ------------------------------------------------------------------
    # 备件库存
    # ------------------------------------------------------------------

    def inbound_stock(self, part_serial: str, quantity: int, operator: str,
                      occurred_at: str | None = None) -> Event:
        part_serial = self._require_text(part_serial, "部件序列号")
        if quantity <= 0:
            raise ValidationFailure("入库数量必须为正")
        with self._lock:
            return self._append(
                "StockInbound", part_serial, occurred_at or utc_now_iso(),
                self._require_text(operator, "操作人"), {"quantity": quantity})

    # ------------------------------------------------------------------
    # 传感读数（不可变证据）
    # ------------------------------------------------------------------

    def ingest_reading(self, cmd: IngestReading) -> Event:
        equipment_serial = self._require_text(cmd.equipment_serial, "设备序列号")
        sensor = self._require_text(cmd.sensor, "传感器")
        if not cmd.values:
            raise ValidationFailure("读数内容不能为空")
        observed = parse_iso(cmd.observed_at).isoformat()
        recorded = parse_iso(cmd.recorded_at or utc_now_iso()).isoformat()
        if parse_iso(observed) > parse_iso(recorded):
            raise ValidationFailure("观测时间不能晚于入库时间")
        with self._lock:
            self._equipment(equipment_serial)
            key = (equipment_serial, sensor, observed)
            if key in self.projection.reading_index:
                # 同一传感器同一时刻读数天然幂等，重复上报直接返回既有事件。
                reading_id = next(
                    r.reading_id for r in self.projection.readings.values()
                    if (r.equipment_serial, r.sensor, r.observed_at) == key)
                eid = self._event_id_of_reading(reading_id)
                return self.projection.events_by_id[eid]
            if parse_iso(recorded) - parse_iso(observed) > READING_BACKFILL_GRACE \
                    and not cmd.backfill:
                raise BackfillRejectedError(
                    "读数入库晚于观测超过 24 小时，必须显式 backfill=true")
            digest = hashlib.sha256(json.dumps(
                {"equipment": equipment_serial, "sensor": sensor,
                 "observed_at": observed, "values": cmd.values},
                ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            reading_id = f"RD-{digest[:16]}"
            return self._append(
                "ReadingIngested", equipment_serial, recorded, "ingest-pipeline",
                {"reading_id": reading_id, "sensor": sensor, "observed_at": observed,
                 "values": dict(cmd.values), "source_uri": cmd.source_uri,
                 "digest": digest},
                backfill=cmd.backfill,
            )

    def _event_id_of_reading(self, reading_id: str) -> str:  # 供幂等返回使用
        for e in self.projection.events_by_id.values():
            if e.event_type == "ReadingIngested" and e.payload.get("reading_id") == reading_id:
                return e.event_id
        raise KeyError(reading_id)

    # ------------------------------------------------------------------
    # 报修受理（重复工单 / 离线补录 / 版本快照）
    # ------------------------------------------------------------------

    def report_fault(self, cmd: ReportFault) -> Event:
        equipment_serial = self._require_text(cmd.equipment_serial, "设备序列号")
        fault_code = self._require_text(cmd.fault_code, "故障代码")
        title = self._require_text(cmd.title, "故障标题")
        reporter = self._require_text(cmd.reporter, "报修人")
        if cmd.role not in {r.value for r in Role}:
            raise ValidationFailure(f"角色非法：{cmd.role}")
        if cmd.severity not in {s.value for s in FaultSeverity}:
            raise ValidationFailure(f"严重度非法：{cmd.severity}")
        with self._lock:
            eq = self._equipment(equipment_serial)
            occurred = self._check_backfill(cmd.occurred_at, cmd.backfill)
            if parse_iso(occurred) < parse_iso(eq.delivered_at):
                raise ValidationFailure("故障时间不能早于设备交付时间")
            if cmd.idempotency_key:
                cached = self._idem(("fault", cmd.idempotency_key))
                if cached is not None:
                    return cached
            for rid in cmd.reading_ids:
                reading = self.projection.readings.get(rid)
                if reading is None:
                    raise NotFoundError(f"读数不存在：{rid}")
                if reading.equipment_serial != equipment_serial:
                    raise ConflictError(f"读数 {rid} 不属于该设备")
                if parse_iso(reading.observed_at) > parse_iso(occurred):
                    raise ConflictError(f"读数 {rid} 观测时间晚于故障时间，不能作为证据")
            # 重复工单：同设备同故障且未关闭 → 明确引导到既有工单。
            existing = self.projection.active_fault(equipment_serial, fault_code)
            if existing is not None:
                raise DuplicateWorkOrderError(
                    f"同一故障 {fault_code} 已有未关闭工单 {existing.work_order_id}"
                    f"（阶段 {existing.stage}），请勿重复建单",
                    existing.work_order_id,
                )
            # 离线补录不得改写已终态的历史结论。
            if cmd.backfill:
                for wo in self.projection.work_orders.values():
                    if (wo.equipment_serial == equipment_serial
                            and wo.fault_code == fault_code and wo.is_terminal
                            and parse_iso(wo.occurred_at) >= parse_iso(occurred)):
                        raise BackfillRejectedError(
                            f"故障 {fault_code} 已有终态工单 {wo.work_order_id}，"
                            "其报修时间不早于本次补录，补录会改写历史结论")
            number = sum(1 for wo in self.projection.work_orders.values()
                         if wo.equipment_serial == equipment_serial) + 1
            wo_id = f"WO-{equipment_serial}-{number:03d}"
            snapshot = self.projection.bom_at(equipment_serial, occurred)
            config_version = self.projection.config_version_at(equipment_serial, occurred)
            payload: dict[str, Any] = {
                "equipment_serial": equipment_serial, "fault_code": fault_code,
                "title": title, "severity": cmd.severity,
                "description": cmd.description, "reporter": reporter,
                "reading_ids": list(cmd.reading_ids),
                "config_version_at_report": config_version,
                "config_snapshot": snapshot,
            }
            if cmd.idempotency_key:
                payload["idempotency_key"] = cmd.idempotency_key
            return self._append("WorkOrderReported", wo_id, occurred, reporter,
                                payload, backfill=cmd.backfill)

    # ------------------------------------------------------------------
    # 工单处置动作（角色 × 阶段权限矩阵）
    # ------------------------------------------------------------------

    def act(self, cmd: WorkOrderAction) -> Event:
        with self._lock:
            wo = self._work_order(cmd.work_order_id)
            # 幂等重试最先处理：工单可能已因上次请求推进到下一阶段。
            if cmd.idempotency_key:
                cached = self._idem((wo.work_order_id, cmd.idempotency_key))
                if cached is not None:
                    return cached
            if wo.is_terminal:
                raise ConflictError(f"工单已{wo.stage}，不可继续处置")
            authorize(cmd.action, cmd.role, wo.stage)
            self._ensure_team(wo, cmd.action, cmd.role)
            occurred = self._check_backfill(cmd.occurred_at, cmd.backfill)
            self._ensure_timeline_order(wo, occurred)
            handler = getattr(self, f"_do_{cmd.action}")
            return handler(wo, cmd, occurred)

    def _ensure_team(self, wo, action: str, role: str) -> None:
        if role == Role.SUPERVISOR.value:
            return  # 主管跨团队处置
        if role == Role.DISPATCH.value and action in (Action.CANCEL, Action.TRANSFER):
            return  # 客服代表客户受理撤单/派单，不受当前归属团队限制
        team_of_role = {
            Role.DISPATCH.value: "dispatch", Role.DIAGNOSTIC.value: "diagnostic",
            Role.PARTS.value: "parts", Role.REPAIR.value: "repair",
        }.get(role)
        if team_of_role and wo.team != team_of_role:
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(
                f"工单当前归属 {wo.team} 团队，{role} 角色需经转交后才能处置")

    def _payload_with_idem(self, cmd: WorkOrderAction, extra: dict[str, Any]) -> dict[str, Any]:
        if cmd.idempotency_key:
            extra["idempotency_key"] = cmd.idempotency_key
        return extra

    # -- 诊断 -------------------------------------------------------------

    def _do_diagnose(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        verdicts = {v.value for v in DiagnosisVerdict}
        if cmd.verdict not in verdicts:
            raise ValidationFailure(f"诊断结论非法：{cmd.verdict}")
        for rid in cmd.reading_ids:
            reading = self.projection.readings.get(rid)
            if reading is None:
                raise NotFoundError(f"读数不存在：{rid}")
            if reading.equipment_serial != wo.equipment_serial:
                raise ConflictError(f"读数 {rid} 不属于该设备")
        if not cmd.reading_ids and not wo.reading_ids:
            raise ValidationFailure("诊断必须引用至少一条原始传感读数作为证据")
        return self._append(
            "WorkOrderDiagnosed", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {
                "verdict": cmd.verdict, "root_cause": cmd.root_cause,
                "reading_ids": list(cmd.reading_ids or wo.reading_ids),
            }), backfill=cmd.backfill)

    # -- 转交 -------------------------------------------------------------

    def _do_transfer(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        to_team = self._require_text(cmd.to_team, "目标团队")
        if to_team not in KNOWN_TEAMS:
            raise ValidationFailure(f"目标团队非法：{to_team}")
        if to_team == wo.team:
            raise ConflictError(f"工单已归属 {wo.team}，无需转交")
        return self._append(
            "WorkOrderTransferred", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {"to_team": to_team, "note": cmd.note}))

    # -- 备件 -------------------------------------------------------------

    def _do_request_parts(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        if not cmd.required_parts:
            raise ValidationFailure("备件清单不能为空")
        parts = []
        for part_serial, slot in cmd.required_parts:
            part_serial = self._require_text(part_serial, "部件序列号")
            slot = self._require_text(slot, "槽位")
            if part_serial in wo.parts:
                raise ConflictError(f"部件 {part_serial} 已在工单备件清单中")
            parts.append({"part_serial": part_serial, "slot": slot})
        return self._append(
            "PartsRequested", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {"parts": parts}), backfill=cmd.backfill)

    def _do_reserve_part(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        req = self._part_req(wo, cmd.part_serial)
        if req.status != PartRequestStatus.REQUESTED.value:
            raise ConflictError(f"部件状态为 {req.status}，不能预留（仅已申请可预留）")
        available = self.projection.stock.get(req.part_serial, 0)
        if available <= 0:
            raise ConflictError(
                f"部件 {req.part_serial} 现货不足（可用 0），"
                "备件团队应登记缺货补订 report_shortage 并给出预计到货，而不是虚假预留")
        return self._append(
            "PartReserved", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {"part_serial": req.part_serial,
                                          "from_stock": True}))

    def _do_report_shortage(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        req = self._part_req(wo, cmd.part_serial)
        if req.status not in (PartRequestStatus.REQUESTED.value,
                              PartRequestStatus.BACKORDERED.value):
            raise ConflictError(f"部件状态为 {req.status}，不能登记缺货")
        eta = self._require_text(cmd.eta, "预计到货时间")
        if parse_iso(eta) <= parse_iso(occurred):
            raise ValidationFailure("预计到货时间必须晚于登记时间")
        return self._append(
            "PartShortage", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {"part_serial": req.part_serial, "eta": eta}))

    def _do_part_arrived(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        req = self._part_req(wo, cmd.part_serial)
        if req.status != PartRequestStatus.BACKORDERED.value:
            raise ConflictError(f"部件状态为 {req.status}，只有缺货补订件需要到货确认")
        return self._append(
            "PartArrived", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {"part_serial": req.part_serial}))

    def _do_cancel_part(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        req = self._part_req(wo, cmd.part_serial)
        if req.status == PartRequestStatus.FULFILLED.value:
            raise ConflictError("部件已出库装机，不能取消申请；如确需拆回应走新的换件记录")
        if req.status == PartRequestStatus.CANCELED.value:
            raise ConflictError("该部件申请已取消")
        return self._append(
            "PartCanceled", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {"part_serial": req.part_serial,
                                          "note": cmd.note}))

    def _do_dispatch_part(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        req = self._part_req(wo, cmd.part_serial)
        if req.status != PartRequestStatus.RESERVED.value:
            raise ConflictError(f"部件状态为 {req.status}，已预留才能出库")
        return self._append(
            "PartDispatched", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {"part_serial": req.part_serial}))

    def _part_req(self, wo, part_serial: str | None):
        part_serial = self._require_text(part_serial, "部件序列号")
        req = wo.parts.get(part_serial)
        if req is None:
            raise NotFoundError(f"工单未申请部件：{part_serial}")
        return req

    # -- 现场维修 / 修复 / 关闭 -------------------------------------------

    def _do_start_repair(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        ready, pending = parts_ready_for_repair(wo.parts)
        if not ready:
            raise ConflictError(
                f"备件尚未齐备（{', '.join(pending)} 仍在申请/缺货中），不能开始现场维修")
        return self._append(
            "RepairStarted", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {"note": cmd.note}), backfill=cmd.backfill)

    def _do_resolve(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        resolution = self._require_text(cmd.resolution, "修复说明")
        outstanding = [s for s, r in wo.parts.items()
                       if r.status not in (PartRequestStatus.FULFILLED.value,
                                           PartRequestStatus.CANCELED.value)]
        if outstanding:
            raise ConflictError(f"备件流程未完结（{', '.join(outstanding)}），不能登记修复")
        # 费用归属按“故障发生时刻”的保修状态判定，后续合同变化不追溯。
        coverage, basis = self.projection.coverage_at(wo.equipment_serial, wo.occurred_at)
        replaced = [
            {"slot": e.slot, "old_part": e.old_part, "new_part": e.new_part,
             "occurred_at": e.occurred_at}
            for slot_entries in self._equipment(wo.equipment_serial).lineage.values()
            for e in slot_entries if e.work_order_id == wo.work_order_id
        ]
        return self._append(
            "WorkOrderResolved", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {
                "resolution": resolution, "coverage": coverage,
                "coverage_basis": basis, "replaced_parts": replaced,
            }), backfill=cmd.backfill)

    def _do_close(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        return self._append(
            "WorkOrderClosed", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {"note": cmd.note}))

    def _do_cancel(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        note = self._require_text(cmd.note, "取消原因")
        return self._append(
            "WorkOrderCanceled", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {"note": note}))

    def _do_force_stage(self, wo, cmd: WorkOrderAction, occurred: str) -> Event:
        target = self._require_text(cmd.target_stage, "目标阶段")
        try:
            WorkOrderStage(target)
        except ValueError:
            raise ValidationFailure(f"目标阶段非法：{target}")
        ensure_transition(wo.stage, target, supervisor=True)
        return self._append(
            "StageForced", wo.work_order_id, occurred, cmd.operator,
            self._payload_with_idem(cmd, {"target_stage": target, "note": cmd.note}))

    # ------------------------------------------------------------------
    # 查询 / 重建
    # ------------------------------------------------------------------

    def list_open_work_orders(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._work_order_dict(wo)
                    for wo in self.projection.work_orders.values()
                    if not wo.is_terminal]

    def equipment_view(self, serial: str) -> dict[str, Any]:
        with self._lock:
            eq = self._equipment(serial)
            coverage, basis = self.projection.coverage_at(serial, utc_now_iso())
            return {
                "serial": eq.serial, "model": eq.model, "customer": eq.customer,
                "delivered_at": eq.delivered_at, "installed_at": eq.installed_at,
                "site": eq.site, "config_version": eq.config_version,
                "current_topology": dict(eq.bom),
                "coverage_now": coverage, "coverage_basis": basis,
                "coverage_history": [c.__dict__ for c in eq.coverage_history],
                "part_lineage": {
                    slot: [e.__dict__ for e in entries]
                    for slot, entries in eq.lineage.items()},
            }

    def work_order_view(self, wo_id: str) -> dict[str, Any]:
        with self._lock:
            return self._work_order_dict(self._work_order(wo_id))

    def _work_order_dict(self, wo) -> dict[str, Any]:
        return {
            "work_order_id": wo.work_order_id,
            "equipment_serial": wo.equipment_serial,
            "fault_code": wo.fault_code, "title": wo.title,
            "severity": wo.severity, "stage": wo.stage, "team": wo.team,
            "reporter": wo.reporter, "backfill": wo.backfill,
            "occurred_at": wo.occurred_at, "recorded_at": wo.recorded_at,
            "reading_ids": list(wo.reading_ids),
            "config_version_at_report": wo.config_version_at_report,
            "diagnosis": wo.diagnosis,
            "parts": {s: {"slot": r.slot, "status": r.status, "eta": r.eta,
                          "history": r.history}
                      for s, r in wo.parts.items()},
            "transfers": wo.transfers,
            "resolution": wo.resolution, "coverage_decision": wo.coverage_decision,
            "resolved_at": wo.resolved_at, "closed_at": wo.closed_at,
            "canceled_at": wo.canceled_at, "cancel_reason": wo.cancel_reason,
        }

    def reconstruct_repair(self, wo_id: str) -> dict[str, Any]:
        """重建一次维修的完整经过：设备版本快照 → 原始读数 → 诊断 → 流转 → 换件 → 结论。"""
        with self._lock:
            wo = self._work_order(wo_id)
            eq = self._equipment(wo.equipment_serial)
            coverage_at_fault, basis = self.projection.coverage_at(
                wo.equipment_serial, wo.occurred_at)
            readings = []
            for rid in wo.reading_ids + (wo.diagnosis["reading_ids"] if wo.diagnosis else []):
                r = self.projection.readings.get(rid)
                if r and not any(x["reading_id"] == rid for x in readings):
                    readings.append({
                        "reading_id": r.reading_id, "sensor": r.sensor,
                        "observed_at": r.observed_at, "recorded_at": r.recorded_at,
                        "values": r.values, "source_uri": r.source_uri,
                        "digest": r.digest, "backfill": r.backfill,
                    })
            return {
                "work_order": self._work_order_dict(wo),
                "equipment": {"serial": eq.serial, "model": eq.model,
                              "customer": eq.customer, "site": eq.site},
                "equipment_version_at_fault": {
                    "config_version": wo.config_version_at_report,
                    "topology": self.projection.bom_at(eq.serial, wo.occurred_at),
                    "current_config_version": eq.config_version,
                },
                "coverage_at_fault": {"coverage": coverage_at_fault, "basis": basis},
                "raw_readings": readings,
                "transfers": wo.transfers,
                "timeline": wo.timeline,
                "evidence_chain": {
                    "first_seq": wo.timeline[0]["seq"] if wo.timeline else None,
                    "last_seq": wo.timeline[-1]["seq"] if wo.timeline else None,
                    "event_count": len(wo.timeline),
                    "log_tail_seq": self.projection.head_seq,
                },
            }
