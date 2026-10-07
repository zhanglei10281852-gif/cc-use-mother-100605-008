"""读模型投影：把事件流重放成可查询状态。

只有 ``apply(event)`` 一个入口，实时追加与启动重放走同一条路径，
保证“重启后看到的世界”与事件日志严格一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .events import Event, parse_iso
from .models import ContractCoverage, PartRequestStatus, WorkOrderStage
from .policy import STAGE_TEAM


@dataclass
class PartLineageEntry:
    slot: str
    old_part: str | None
    new_part: str
    occurred_at: str
    recorded_at: str
    operator: str
    work_order_id: str | None
    seq: int


@dataclass
class CoverageChange:
    occurred_at: str
    coverage: str
    warranty_end: str | None
    reason: str
    operator: str
    seq: int


@dataclass
class EquipmentState:
    serial: str
    model: str
    customer: str
    delivered_at: str
    warranty_months: int
    installed_at: str | None = None
    site: str | None = None
    bom: dict[str, str] = field(default_factory=dict)               # slot -> 当前部件序列号
    config_version: int = 0
    lineage: dict[str, list[PartLineageEntry]] = field(default_factory=dict)
    coverage_history: list[CoverageChange] = field(default_factory=list)
    reading_ids: list[str] = field(default_factory=list)


@dataclass
class ReadingState:
    reading_id: str
    equipment_serial: str
    sensor: str
    observed_at: str
    recorded_at: str
    values: dict[str, float]
    source_uri: str
    digest: str
    backfill: bool


@dataclass
class PartRequest:
    part_serial: str
    slot: str
    status: str
    eta: str | None = None
    from_stock: bool = False   # 预留是否占用了现货（取消时需回补）
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class WorkOrderState:
    work_order_id: str
    equipment_serial: str
    fault_code: str
    title: str
    severity: str
    description: str
    reporter: str
    team: str
    stage: str
    occurred_at: str
    recorded_at: str
    backfill: bool
    reading_ids: list[str] = field(default_factory=list)
    config_version_at_report: int = 0
    diagnosis: dict[str, Any] | None = None
    parts: dict[str, PartRequest] = field(default_factory=dict)
    transfers: list[dict[str, Any]] = field(default_factory=list)
    resolution: str = ""
    coverage_decision: dict[str, Any] | None = None
    resolved_at: str | None = None
    closed_at: str | None = None
    canceled_at: str | None = None
    cancel_reason: str = ""
    timeline: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.stage in (WorkOrderStage.CLOSED.value, WorkOrderStage.CANCELED.value)


class Projection:
    def __init__(self) -> None:
        self.equipment: dict[str, EquipmentState] = {}
        self.readings: dict[str, ReadingState] = {}
        self.reading_index: set[tuple[str, str, str]] = set()  # (设备, 传感器, 观测时刻)
        self.work_orders: dict[str, WorkOrderState] = {}
        self.stock: dict[str, int] = {}                        # 现货（已入库未出库）
        self.installed_parts: dict[str, tuple[str, str]] = {}  # 部件序列号 -> (设备, slot)
        self.events_by_id: dict[str, Event] = {}
        self.idem_keys: dict[tuple[str, str], str] = {}        # 去重键 -> event_id
        self.head_seq = 0

    # -- 应用 --------------------------------------------------------------

    def apply(self, e: Event) -> None:
        self.head_seq = e.seq
        self.events_by_id[e.event_id] = e
        p = e.payload
        idem = p.get("idempotency_key")
        if idem:
            scope = ("fault", idem) if e.event_type == "WorkOrderReported" \
                else (e.aggregate_id, idem)
            self.idem_keys.setdefault(scope, e.event_id)
        handler = getattr(self, f"_on_{e.event_type}", None)
        if handler is not None:
            handler(e, p)

    def _log(self, wo: WorkOrderState, e: Event, title: str, stage: str | None = None,
             **details: Any) -> None:
        wo.timeline.append({
            "seq": e.seq,
            "occurred_at": e.occurred_at,
            "recorded_at": e.recorded_at,
            "type": e.event_type,
            "title": title,
            "operator": e.operator,
            "team": wo.team,
            "stage_after": stage or wo.stage,
            "backfill": e.backfill,
            "details": details,
        })

    # -- 设备/部件 ----------------------------------------------------------

    def _on_EquipmentRegistered(self, e: Event, p: dict) -> None:
        eq = EquipmentState(
            serial=e.aggregate_id, model=p["model"], customer=p["customer"],
            delivered_at=e.occurred_at, warranty_months=p["warranty_months"],
            bom=dict(p["bill_of_materials"]),
        )
        self.equipment[eq.serial] = eq
        for slot, part in dict(p["bill_of_materials"]).items():
            self.installed_parts[part] = (eq.serial, slot)
            eq.lineage.setdefault(slot, []).append(PartLineageEntry(
                slot=slot, old_part=None, new_part=part, occurred_at=e.occurred_at,
                recorded_at=e.recorded_at, operator=e.operator,
                work_order_id=None, seq=e.seq,
            ))
            eq.config_version += 1

    def _on_EquipmentInstalled(self, e: Event, p: dict) -> None:
        eq = self.equipment[e.aggregate_id]
        eq.installed_at = e.occurred_at
        eq.site = p["site"]

    def _on_PartReplaced(self, e: Event, p: dict) -> None:
        eq = self.equipment[e.aggregate_id]
        slot = p["slot"]
        old = p["old_part_serial"]
        new = p["new_part_serial"]
        if old is not None:
            self.installed_parts.pop(old, None)
        eq.bom[slot] = new
        self.installed_parts[new] = (eq.serial, slot)
        eq.config_version += 1
        eq.lineage.setdefault(slot, []).append(PartLineageEntry(
            slot=slot, old_part=old, new_part=new, occurred_at=e.occurred_at,
            recorded_at=e.recorded_at, operator=e.operator,
            work_order_id=p.get("work_order_id"), seq=e.seq,
        ))
        wo_id = p.get("work_order_id")
        if wo_id and wo_id in self.work_orders:
            wo = self.work_orders[wo_id]
            wo.timeline.append({
                "seq": e.seq, "occurred_at": e.occurred_at,
                "recorded_at": e.recorded_at, "type": e.event_type,
                "title": f"部件更换：{slot} {old or '空'} → {new}",
                "operator": e.operator, "team": wo.team,
                "stage_after": wo.stage, "backfill": e.backfill,
                "details": {"slot": slot, "old_part": old, "new_part": new},
            })

    def _on_WarrantyChanged(self, e: Event, p: dict) -> None:
        eq = self.equipment[e.aggregate_id]
        eq.coverage_history.append(CoverageChange(
            occurred_at=e.occurred_at, coverage=p["new_coverage"],
            warranty_end=p.get("warranty_end"), reason=p["reason"],
            operator=e.operator, seq=e.seq,
        ))

    # -- 传感读数 -----------------------------------------------------------

    def _on_ReadingIngested(self, e: Event, p: dict) -> None:
        r = ReadingState(
            reading_id=p["reading_id"], equipment_serial=e.aggregate_id,
            sensor=p["sensor"], observed_at=p["observed_at"],
            recorded_at=e.recorded_at, values=dict(p["values"]),
            source_uri=p.get("source_uri", ""), digest=p["digest"],
            backfill=e.backfill,
        )
        self.readings[r.reading_id] = r
        self.reading_index.add((r.equipment_serial, r.sensor, r.observed_at))
        self.equipment[r.equipment_serial].reading_ids.append(r.reading_id)

    # -- 库存 ---------------------------------------------------------------

    def _on_StockInbound(self, e: Event, p: dict) -> None:
        self.stock[e.aggregate_id] = self.stock.get(e.aggregate_id, 0) + p["quantity"]

    # -- 工单 ---------------------------------------------------------------

    def _on_WorkOrderReported(self, e: Event, p: dict) -> None:
        eq = self.equipment[p["equipment_serial"]]
        wo = WorkOrderState(
            work_order_id=e.aggregate_id, equipment_serial=p["equipment_serial"],
            fault_code=p["fault_code"], title=p["title"], severity=p["severity"],
            description=p.get("description", ""), reporter=p["reporter"],
            team="dispatch", stage=WorkOrderStage.OPEN.value,
            occurred_at=e.occurred_at, recorded_at=e.recorded_at, backfill=e.backfill,
            reading_ids=list(p.get("reading_ids", ())),
            config_version_at_report=p.get("config_version_at_report", eq.config_version),
        )
        self.work_orders[wo.work_order_id] = wo
        self._log(wo, e, f"受理报修 {wo.fault_code}：{wo.title}")

    def _on_WorkOrderDiagnosed(self, e: Event, p: dict) -> None:
        wo = self._wo(e)
        wo.team = "diagnostic"
        wo.stage = WorkOrderStage.DIAGNOSED.value
        wo.diagnosis = {
            "verdict": p["verdict"], "root_cause": p.get("root_cause", ""),
            "reading_ids": list(p.get("reading_ids", ())),
            "occurred_at": e.occurred_at, "operator": e.operator, "seq": e.seq,
        }
        self._log(wo, e, f"诊断结论：{p['verdict']}", verdict=p["verdict"],
                  root_cause=p.get("root_cause", ""),
                  reading_ids=list(p.get("reading_ids", ())))

    def _on_WorkOrderTransferred(self, e: Event, p: dict) -> None:
        wo = self._wo(e)
        transfer = {"from_team": wo.team, "to_team": p["to_team"],
                    "occurred_at": e.occurred_at, "operator": e.operator,
                    "reason": p.get("note", ""), "seq": e.seq}
        wo.transfers.append(transfer)
        wo.team = p["to_team"]
        self._log(wo, e, f"跨团队转交：{transfer['from_team']} → {wo.team}", **transfer)

    def _on_PartsRequested(self, e: Event, p: dict) -> None:
        wo = self._wo(e)
        wo.stage = WorkOrderStage.WAITING_PARTS.value
        wo.team = "parts"
        for item in p["parts"]:
            req = PartRequest(part_serial=item["part_serial"], slot=item["slot"],
                              status=PartRequestStatus.REQUESTED.value)
            req.history.append({"status": req.status, "occurred_at": e.occurred_at,
                                "operator": e.operator, "seq": e.seq})
            wo.parts[req.part_serial] = req
        self._log(wo, e, f"申请备件 {len(p['parts'])} 项",
                  parts=[f"{i['part_serial']}@{i['slot']}" for i in p["parts"]])

    def _part_status(self, wo: WorkOrderState, e: Event, p: dict, new_status: str,
                     title: str) -> None:
        req = wo.parts[p["part_serial"]]
        req.status = new_status
        if "eta" in p:
            req.eta = p["eta"]
        req.history.append({"status": new_status, "occurred_at": e.occurred_at,
                            "operator": e.operator, "seq": e.seq,
                            "eta": p.get("eta")})
        self._log(wo, e, title, part=p["part_serial"], eta=p.get("eta"))

    def _on_PartReserved(self, e: Event, p: dict) -> None:
        wo = self._wo(e)
        req = wo.parts[p["part_serial"]]
        if p.get("from_stock"):
            req.from_stock = True
            self.stock[p["part_serial"]] = self.stock.get(p["part_serial"], 0) - 1
        req.status = PartRequestStatus.RESERVED.value
        req.history.append({"status": req.status, "occurred_at": e.occurred_at,
                            "operator": e.operator, "seq": e.seq,
                            "from_stock": req.from_stock})
        self._log(wo, e, f"备件已预留：{p['part_serial']}"
                  + ("（占用现货）" if req.from_stock else "（补订到货）"),
                  part=p["part_serial"], from_stock=req.from_stock)

    def _on_PartShortage(self, e: Event, p: dict) -> None:
        self._part_status(self._wo(e), e, p, PartRequestStatus.BACKORDERED.value,
                          f"备件缺货补订：{p['part_serial']}")

    def _on_PartArrived(self, e: Event, p: dict) -> None:
        wo = self._wo(e)
        req = wo.parts[p["part_serial"]]
        # 补订到货直接预留给本工单，不进入可分配现货池。
        req.status = PartRequestStatus.RESERVED.value
        req.eta = None
        req.from_stock = False
        req.history.append({"status": req.status, "occurred_at": e.occurred_at,
                            "operator": e.operator, "seq": e.seq,
                            "from_stock": False})
        self._log(wo, e, f"备件到货并预留：{p['part_serial']}", part=p["part_serial"])

    def _on_PartCanceled(self, e: Event, p: dict) -> None:
        wo = self._wo(e)
        req = wo.parts[p["part_serial"]]
        if req.status == PartRequestStatus.RESERVED.value and req.from_stock:
            self.stock[req.part_serial] = self.stock.get(req.part_serial, 0) + 1
        req.status = PartRequestStatus.CANCELED.value
        req.history.append({"status": req.status, "occurred_at": e.occurred_at,
                            "operator": e.operator, "seq": e.seq,
                            "note": p.get("note", "方案变更，取消备件申请")})
        self._log(wo, e, f"取消备件申请：{p['part_serial']}", part=p["part_serial"],
                  note=p.get("note", ""))

    def _on_PartDispatched(self, e: Event, p: dict) -> None:
        wo = self._wo(e)
        req = wo.parts[p["part_serial"]]
        req.status = PartRequestStatus.FULFILLED.value
        req.history.append({"status": req.status, "occurred_at": e.occurred_at,
                            "operator": e.operator, "seq": e.seq})
        # 现货在预留时已扣减；补订件从未入池，出库均不再改 stock。
        self._log(wo, e, f"备件出库：{p['part_serial']}（装入 {req.slot}）",
                  part=p["part_serial"], slot=req.slot)

    def _on_RepairStarted(self, e: Event, p: dict) -> None:
        wo = self._wo(e)
        wo.team = "repair"
        wo.stage = WorkOrderStage.IN_REPAIR.value
        self._log(wo, e, "现场维修开始", note=p.get("note", ""))

    def _on_WorkOrderResolved(self, e: Event, p: dict) -> None:
        wo = self._wo(e)
        wo.team = "dispatch"
        wo.stage = WorkOrderStage.RESOLVED.value
        wo.resolved_at = e.occurred_at
        wo.resolution = p["resolution"]
        wo.coverage_decision = {
            "coverage": p["coverage"], "basis": p.get("coverage_basis", ""),
            "operator": e.operator, "occurred_at": e.occurred_at,
        }
        self._log(wo, e, f"维修完成：{p['resolution']}", coverage=p["coverage"],
                  replaced_parts=list(p.get("replaced_parts", ())))

    def _on_WorkOrderClosed(self, e: Event, p: dict) -> None:
        wo = self._wo(e)
        wo.stage = WorkOrderStage.CLOSED.value
        wo.closed_at = e.occurred_at
        self._log(wo, e, "客服核验关闭", note=p.get("note", ""))

    def _on_WorkOrderCanceled(self, e: Event, p: dict) -> None:
        wo = self._wo(e)
        wo.stage = WorkOrderStage.CANCELED.value
        wo.canceled_at = e.occurred_at
        wo.cancel_reason = p.get("note", "")
        for req in wo.parts.values():
            if req.status in (PartRequestStatus.REQUESTED.value,
                              PartRequestStatus.RESERVED.value,
                              PartRequestStatus.BACKORDERED.value):
                if req.status == PartRequestStatus.RESERVED.value and req.from_stock:
                    self.stock[req.part_serial] = self.stock.get(req.part_serial, 0) + 1
                req.status = PartRequestStatus.CANCELED.value
                req.history.append({"status": req.status, "occurred_at": e.occurred_at,
                                    "operator": e.operator, "seq": e.seq,
                                    "note": "工单取消，释放预留"})
        self._log(wo, e, f"工单取消：{wo.cancel_reason}")

    def _on_StageForced(self, e: Event, p: dict) -> None:
        wo = self._wo(e)
        old = wo.stage
        wo.stage = p["target_stage"]
        # 阶段被强制退回/推进时，归属团队同步到该阶段的责任团队。
        wo.team = STAGE_TEAM.get(wo.stage, wo.team)
        self._log(wo, e, f"主管强制流转：{old} → {wo.stage}",
                  from_stage=old, to_stage=wo.stage, reason=p.get("note", ""))

    def _wo(self, e: Event) -> WorkOrderState:
        return self.work_orders[e.aggregate_id]

    # -- 查询辅助 -----------------------------------------------------------

    def equipment_or_none(self, serial: str) -> EquipmentState | None:
        return self.equipment.get(serial)

    def work_order_or_none(self, wo_id: str) -> WorkOrderState | None:
        return self.work_orders.get(wo_id)

    def active_fault(self, equipment_serial: str, fault_code: str) -> WorkOrderState | None:
        """同一设备、同一故障代码且未关闭的工单（重复工单判定）。"""
        for wo in self.work_orders.values():
            if (wo.equipment_serial == equipment_serial
                    and wo.fault_code == fault_code
                    and not wo.is_terminal):
                return wo
        return None

    def coverage_at(self, equipment_serial: str, at_iso: str) -> tuple[str, str]:
        """故障发生时刻 T 适用的保修范围——只看 occurred_at <= T 的合同变更。

        之后发生的保修缩水/脱保不改变历史故障的费用归属，保证争议可追溯。
        返回 (coverage, 依据说明)。
        """
        eq = self.equipment[equipment_serial]
        at_dt = parse_iso(at_iso)
        effective = None
        for change in sorted(eq.coverage_history, key=lambda c: (c.occurred_at, c.seq)):
            if parse_iso(change.occurred_at) <= at_dt:
                effective = change
        if effective is None:
            end = parse_iso(eq.delivered_at) + timedelta(days=365 * eq.warranty_months / 12)
            if at_dt <= end:
                return ContractCoverage.WARRANTY.value, \
                    f"交付日 {eq.delivered_at} 起 {eq.warranty_months} 个月保修期内"
            return ContractCoverage.OUT_OF_WARRANTY.value, \
                f"保修期已于 {end.isoformat()} 届满"
        if effective.coverage == ContractCoverage.WARRANTY.value and effective.warranty_end:
            if at_dt <= parse_iso(effective.warranty_end):
                return ContractCoverage.WARRANTY.value, \
                    f"保修变更至 {effective.warranty_end}（{effective.reason}）"
            return ContractCoverage.OUT_OF_WARRANTY.value, \
                f"变更后保修期已于 {effective.warranty_end} 届满"
        return effective.coverage, f"依据 {effective.occurred_at} 的保修范围变更：{effective.reason}"

    def bom_at(self, equipment_serial: str, at_iso: str) -> dict[str, str]:
        """重建某一时刻的设备拓扑（按发生时间回放换件谱系）。"""
        eq = self.equipment[equipment_serial]
        at_dt = parse_iso(at_iso)
        bom: dict[str, str] = {}
        entries: list[PartLineageEntry] = []
        for slot_items in eq.lineage.values():
            entries.extend(slot_items)
        for entry in sorted(entries, key=lambda x: (x.occurred_at, x.seq)):
            if parse_iso(entry.occurred_at) <= at_dt:
                bom[entry.slot] = entry.new_part
        return bom

    def config_version_at(self, equipment_serial: str, at_iso: str) -> int:
        """故障发生时刻的配置版本号：截至该时刻的谱系条目数。"""
        eq = self.equipment[equipment_serial]
        at_dt = parse_iso(at_iso)
        total = 0
        for entries in eq.lineage.values():
            total += sum(1 for e in entries if parse_iso(e.occurred_at) <= at_dt)
        return total
