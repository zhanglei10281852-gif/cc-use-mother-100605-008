"""生命周期服务端到端行为测试。"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from lifecycle_service.errors import (
    BackfillRejectedError,
    ConflictError,
    DuplicateWorkOrderError,
    LogIntegrityError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailure,
)
from lifecycle_service.models import (
    Action,
    ChangeWarranty,
    IngestReading,
    InstallEquipment,
    RegisterEquipment,
    ReplacePart,
    ReportFault,
    Role,
    WorkOrderAction,
)
from lifecycle_service.service import LifecycleService


def iso(dt: datetime) -> str:
    return dt.isoformat()


NOW = datetime.now(timezone.utc)


class LifecycleFixture:
    def __init__(self, path: str):
        self.svc = LifecycleService(path)

    def bootstrap(self, delivered_at: str | None = None):
        self.svc.register_equipment(RegisterEquipment(
            serial="SG-001", model="MVR-200", customer="北方化工厂",
            delivered_at=delivered_at or iso(NOW - timedelta(days=100)),
            operator="sales-li",
            bill_of_materials={"rotor": "P-ROTOR-1", "seal": "P-SEAL-1"},
            warranty_months=12))
        self.svc.install_equipment(InstallEquipment(
            serial="SG-001", site="1号压缩机组", operator="fitter-wang"))
        return self.svc

    def reading(self, sensor="vib-1", observed=None, backfill=False):
        observed = observed or (NOW - timedelta(hours=23))
        return self.svc.ingest_reading(IngestReading(
            equipment_serial="SG-001", sensor=sensor, observed_at=iso(observed),
            values={"rms": 8.4, "peak": 21.0}, source_uri="s3://tele/SG-001/vib-1.cbor",
            backfill=backfill))

    def open_with_diagnosis(self, fault="E1", verdict="confirmed",
                            root_cause="密封磨损"):
        """标准开场：读数 → 受理 → 客服派单诊断团队 → 诊断结论。"""
        ev = self.reading(sensor=f"s-{fault}")
        rid = ev.payload["reading_id"]
        wo_id = self.svc.report_fault(ReportFault(
            equipment_serial="SG-001", fault_code=fault, title="故障",
            reporter="客服-陈", reading_ids=(rid,))).aggregate_id
        self.svc.act(WorkOrderAction(
            wo_id, Action.TRANSFER, "客服-陈", Role.DISPATCH.value,
            to_team="diagnostic"))
        self.svc.act(WorkOrderAction(
            wo_id, Action.DIAGNOSE, "诊断-赵", Role.DIAGNOSTIC.value,
            verdict=verdict, root_cause=root_cause, reading_ids=(rid,)))
        return wo_id, rid


class HappyPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = LifecycleFixture(os.path.join(self.tmp.name, "events.jsonl"))
        self.fx.bootstrap()

    def tearDown(self):
        self.tmp.cleanup()

    def _open_wo(self):
        event = self.fx.reading()
        rid = event.payload["reading_id"]
        reported = self.fx.svc.report_fault(ReportFault(
            equipment_serial="SG-001", fault_code="E-VIB-HIGH", title="振动高位",
            reporter="客服-陈", reading_ids=(rid,)))
        return reported.aggregate_id, rid

    def test_full_repair_lifecycle_and_topology_update(self):
        svc = self.fx.svc
        wo_id, rid = self._open_wo()
        # 客服派单给诊断团队
        svc.act(WorkOrderAction(
            wo_id, Action.TRANSFER, "客服-陈", Role.DISPATCH.value,
            to_team="diagnostic"))
        # 诊断（必须引用原始读数）
        svc.act(WorkOrderAction(
            wo_id, Action.DIAGNOSE, "诊断-赵", Role.DIAGNOSTIC.value,
            verdict="repair_scope", root_cause="密封磨损导致转子碰摩",
            reading_ids=(rid,)))
        # 申请备件
        svc.act(WorkOrderAction(
            wo_id, Action.REQUEST_PARTS, "诊断-赵", Role.DIAGNOSTIC.value,
            required_parts=(("P-SEAL-9", "seal"),)))
        # 无现货：预留必须被拒绝，只能登记缺货
        with self.assertRaises(ConflictError):
            svc.act(WorkOrderAction(
                wo_id, Action.RESERVE_PART, "备件-钱", Role.PARTS.value,
                part_serial="P-SEAL-9"))
        with self.assertRaises(PermissionDeniedError):
            svc.act(WorkOrderAction(
                wo_id, Action.REPORT_SHORTAGE, "诊断-赵", Role.DIAGNOSTIC.value,
                part_serial="P-SEAL-9", eta=iso(NOW + timedelta(days=3))))
        svc.act(WorkOrderAction(
            wo_id, Action.REPORT_SHORTAGE, "备件-钱", Role.PARTS.value,
            part_serial="P-SEAL-9", eta=iso(NOW + timedelta(days=3))))
        # 未到货不能开始维修（主管绕过团队约束，仍受备件齐备门禁拦）
        with self.assertRaises(ConflictError):
            svc.act(WorkOrderAction(
                wo_id, Action.START_REPAIR, "主管-周", Role.SUPERVISOR.value))
        # 到货 → 出库（仍归属备件团队）
        svc.act(WorkOrderAction(
            wo_id, Action.PART_ARRIVED, "备件-钱", Role.PARTS.value,
            part_serial="P-SEAL-9"))
        svc.act(WorkOrderAction(
            wo_id, Action.DISPATCH_PART, "备件-钱", Role.PARTS.value,
            part_serial="P-SEAL-9"))
        # 未转交维修团队前不能开工
        with self.assertRaises(PermissionDeniedError):
            svc.act(WorkOrderAction(
                wo_id, Action.START_REPAIR, "维修-孙", Role.REPAIR.value))
        svc.act(WorkOrderAction(
            wo_id, Action.TRANSFER, "主管-周", Role.SUPERVISOR.value,
            to_team="repair", note="备件齐备，派现场"))
        svc.act(WorkOrderAction(
            wo_id, Action.START_REPAIR, "维修-孙", Role.REPAIR.value))
        # 换件：旧部件不匹配必须拒绝
        with self.assertRaises(ConflictError):
            svc.replace_part(ReplacePart(
                "SG-001", "seal", old_part_serial="P-SEAL-X",
                new_part_serial="P-SEAL-9", operator="维修-孙", work_order_id=wo_id))
        svc.replace_part(ReplacePart(
            "SG-001", "seal", old_part_serial="P-SEAL-1",
            new_part_serial="P-SEAL-9", operator="维修-孙", work_order_id=wo_id))
        # 拓扑已更新
        view = svc.equipment_view("SG-001")
        self.assertEqual(view["current_topology"]["seal"], "P-SEAL-9")
        self.assertEqual(view["current_topology"]["rotor"], "P-ROTOR-1")
        # 修复 → 客服关闭
        svc.act(WorkOrderAction(
            wo_id, Action.RESOLVE, "维修-孙", Role.REPAIR.value,
            resolution="更换密封，振动恢复正常"))
        with self.assertRaises(PermissionDeniedError):
            svc.act(WorkOrderAction(
                wo_id, Action.CLOSE, "维修-孙", Role.REPAIR.value))
        svc.act(WorkOrderAction(
            wo_id, Action.CLOSE, "客服-陈", Role.DISPATCH.value,
            note="客户确认无异响"))

        wo = svc.work_order_view(wo_id)
        self.assertEqual(wo["stage"], "closed")
        self.assertEqual(wo["coverage_decision"]["coverage"], "warranty")
        record = svc.reconstruct_repair(wo_id)
        # 故障时刻拓扑仍是旧密封（服务记录对得上实际设备版本）
        self.assertEqual(record["equipment_version_at_fault"]["topology"]["seal"],
                         "P-SEAL-1")
        self.assertEqual(record["raw_readings"][0]["reading_id"], rid)
        titles = [t["title"] for t in record["timeline"]]
        self.assertTrue(any("缺货补订" in t for t in titles))
        self.assertTrue(any("跨团队转交" in t for t in titles))
        self.assertTrue(any("部件更换" in t and "P-SEAL-9" in t for t in titles))
        self.assertEqual(record["evidence_chain"]["event_count"], len(titles))

    def test_duplicate_work_order_points_to_existing(self):
        wo_id, _ = self._open_wo()
        with self.assertRaises(DuplicateWorkOrderError) as ctx:
            self.fx.svc.report_fault(ReportFault(
                equipment_serial="SG-001", fault_code="E-VIB-HIGH",
                title="又一次振动报警", reporter="客服-陈"))
        self.assertEqual(ctx.exception.existing_work_order_id, wo_id)
        # 关闭后允许重新建单
        self.fx.svc.act(WorkOrderAction(
            wo_id, Action.CANCEL, "客服-陈", Role.DISPATCH.value, note="误报"))
        again = self.fx.svc.report_fault(ReportFault(
            equipment_serial="SG-001", fault_code="E-VIB-HIGH",
            title="再次振动报警", reporter="客服-陈"))
        self.assertNotEqual(again.aggregate_id, wo_id)

    def test_idempotent_retry_returns_same_event(self):
        event1 = self.fx.svc.report_fault(ReportFault(
            equipment_serial="SG-001", fault_code="E1", title="t",
            reporter="客服-陈", idempotency_key="client-abc"))
        event2 = self.fx.svc.report_fault(ReportFault(
            equipment_serial="SG-001", fault_code="E1", title="t",
            reporter="客服-陈", idempotency_key="client-abc"))
        self.assertEqual(event1.event_id, event2.event_id)
        wo_id = event1.aggregate_id
        d1 = self.fx.svc.act(WorkOrderAction(
            wo_id, Action.TRANSFER, "客服-陈", Role.DISPATCH.value,
            to_team="diagnostic", idempotency_key="tx-1"))
        d2 = self.fx.svc.act(WorkOrderAction(
            wo_id, Action.TRANSFER, "客服-陈", Role.DISPATCH.value,
            to_team="diagnostic", idempotency_key="tx-1"))
        self.assertEqual(d1.event_id, d2.event_id)

    def test_reading_ingestion_is_naturally_idempotent(self):
        ts = iso(NOW - timedelta(hours=2))
        e1 = self.fx.svc.ingest_reading(IngestReading(
            equipment_serial="SG-001", sensor="vib-2", observed_at=ts,
            values={"rms": 1.0}))
        e2 = self.fx.svc.ingest_reading(IngestReading(
            equipment_serial="SG-001", sensor="vib-2", observed_at=ts,
            values={"rms": 1.0}))
        self.assertEqual(e1.event_id, e2.event_id)

    def test_force_stage_rolls_team_back_and_backfill_order_enforced(self):
        svc = self.fx.svc
        wo_id, _ = self.fx.open_with_diagnosis(fault="E-FORCE")
        svc.act(WorkOrderAction(
            wo_id, Action.TRANSFER, "主管-周", Role.SUPERVISOR.value, to_team="repair"))
        svc.act(WorkOrderAction(
            wo_id, Action.START_REPAIR, "维修-孙", Role.REPAIR.value))
        svc.act(WorkOrderAction(
            wo_id, Action.RESOLVE, "维修-孙", Role.REPAIR.value, resolution="初修"))
        # 主管返工：阶段与归属团队一并退回维修
        svc.act(WorkOrderAction(
            wo_id, Action.FORCE_STAGE, "主管-周", Role.SUPERVISOR.value,
            target_stage="in_repair", note="复验异响"))
        view = svc.work_order_view(wo_id)
        self.assertEqual(view["stage"], "in_repair")
        self.assertEqual(view["team"], "repair")
        # 关联工单的换件补录不得乱序（早于既有处置时间）
        with self.assertRaises(BackfillRejectedError):
            svc.replace_part(ReplacePart(
                "SG-001", "rotor", old_part_serial="P-ROTOR-1",
                new_part_serial="P-ROTOR-2", operator="维修-孙", work_order_id=wo_id,
                occurred_at=iso(NOW - timedelta(days=2)), backfill=True))
        # 维修角色无需再次转交即可继续处置
        svc.act(WorkOrderAction(
            wo_id, Action.RESOLVE, "维修-孙", Role.REPAIR.value, resolution="复修完成"))


class WarrantyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = LifecycleFixture(os.path.join(self.tmp.name, "events.jsonl"))
        self.fx.bootstrap()

    def tearDown(self):
        self.tmp.cleanup()

    def _quick_wo(self, fault="E1"):
        wo_id, _ = self.fx.open_with_diagnosis(fault=fault)
        self.fx.svc.act(WorkOrderAction(
            wo_id, Action.START_REPAIR, "主管-周", Role.SUPERVISOR.value))
        self.fx.svc.act(WorkOrderAction(
            wo_id, Action.RESOLVE, "维修-孙", Role.REPAIR.value,
            resolution="修复"))
        return wo_id

    def test_scope_decided_by_warranty_state_at_fault_time(self):
        # 故障发生后保修缩水为脱保，费用归属仍按故障时刻——保修内。
        wo_id = self._quick_wo()
        self.fx.svc.change_warranty(ChangeWarranty(
            equipment_serial="SG-001", new_coverage="out_of_warranty",
            operator="主管-周", reason="合同到期未续约"))
        wo = self.fx.svc.work_order_view(wo_id)
        self.assertEqual(wo["coverage_decision"]["coverage"], "warranty")

    def test_out_of_warranty_before_fault_is_chargeable(self):
        self.fx.svc.change_warranty(ChangeWarranty(
            equipment_serial="SG-001", new_coverage="out_of_warranty",
            operator="主管-周", reason="客户原因保修失效",
            occurred_at=iso(NOW - timedelta(days=2))))
        wo_id = self._quick_wo("E2")
        wo = self.fx.svc.work_order_view(wo_id)
        self.assertEqual(wo["coverage_decision"]["coverage"], "out_of_warranty")


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = LifecycleFixture(os.path.join(self.tmp.name, "events.jsonl"))
        self.fx.bootstrap()

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_event_requires_explicit_backfill(self):
        old = iso(NOW - timedelta(days=5))
        with self.assertRaises(BackfillRejectedError):
            self.fx.svc.report_fault(ReportFault(
                equipment_serial="SG-001", fault_code="E-OLD", title="历史故障",
                reporter="客服-陈", occurred_at=old))
        ev = self.fx.svc.report_fault(ReportFault(
            equipment_serial="SG-001", fault_code="E-OLD", title="历史故障",
            reporter="客服-陈", occurred_at=old, backfill=True))
        self.assertTrue(ev.backfill)

    def test_backfill_cannot_be_inserted_out_of_order(self):
        old = iso(NOW - timedelta(days=5))
        wo_id = self.fx.svc.report_fault(ReportFault(
            equipment_serial="SG-001", fault_code="E-OLD2", title="历史故障",
            reporter="客服-陈", occurred_at=old, backfill=True)).aggregate_id
        with self.assertRaises(BackfillRejectedError):
            self.fx.svc.act(WorkOrderAction(
                wo_id, Action.DIAGNOSE, "主管-周", Role.SUPERVISOR.value,
                verdict="confirmed", root_cause="x",
                occurred_at=iso(NOW - timedelta(days=6)), backfill=True))

    def test_backfill_against_terminal_history_is_rejected(self):
        ev = self.fx.reading(observed=NOW - timedelta(days=3), backfill=True)
        rid = ev.payload["reading_id"]
        wo_id = self.fx.svc.report_fault(ReportFault(
            equipment_serial="SG-001", fault_code="E-BF", title="t",
            reporter="客服-陈", reading_ids=(rid,))).aggregate_id
        self.fx.svc.act(WorkOrderAction(
            wo_id, Action.TRANSFER, "客服-陈", Role.DISPATCH.value,
            to_team="diagnostic"))
        self.fx.svc.act(WorkOrderAction(
            wo_id, Action.DIAGNOSE, "诊断-赵", Role.DIAGNOSTIC.value,
            verdict="false_alarm", root_cause="无异常", reading_ids=(rid,)))
        self.fx.svc.act(WorkOrderAction(
            wo_id, Action.CANCEL, "客服-陈", Role.DISPATCH.value, note="误报关闭"))
        # 试图把同故障的报修补录到更早，会改写已关闭结论 → 拒绝
        with self.assertRaises(BackfillRejectedError):
            self.fx.svc.report_fault(ReportFault(
                equipment_serial="SG-001", fault_code="E-BF", title="更早的同故障",
                reporter="客服-陈",
                occurred_at=iso(NOW - timedelta(days=10)), backfill=True))


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = LifecycleFixture(os.path.join(self.tmp.name, "events.jsonl"))
        self.fx.bootstrap()
        self.wo_id = self.fx.svc.report_fault(ReportFault(
            equipment_serial="SG-001", fault_code="E-PERM", title="t",
            reporter="客服-陈")).aggregate_id

    def tearDown(self):
        self.tmp.cleanup()

    def test_stage_gates_actions(self):
        # open 阶段不能关闭/修复
        with self.assertRaises(PermissionDeniedError):
            self.fx.svc.act(WorkOrderAction(
                self.wo_id, Action.CLOSE, "客服-陈", Role.DISPATCH.value))
        with self.assertRaises(PermissionDeniedError):
            self.fx.svc.act(WorkOrderAction(
                self.wo_id, Action.RESOLVE, "维修-孙", Role.REPAIR.value))
        # 只有诊断角色能下诊断结论
        with self.assertRaises(PermissionDeniedError):
            self.fx.svc.act(WorkOrderAction(
                self.wo_id, Action.DIAGNOSE, "客服-陈", Role.DISPATCH.value,
                verdict="confirmed"))

    def test_diagnosis_without_reading_evidence_rejected(self):
        self.fx.svc.act(WorkOrderAction(
            self.wo_id, Action.TRANSFER, "客服-陈", Role.DISPATCH.value,
            to_team="diagnostic"))
        with self.assertRaises(ValidationFailure):
            self.fx.svc.act(WorkOrderAction(
                self.wo_id, Action.DIAGNOSE, "诊断-赵", Role.DIAGNOSTIC.value,
                verdict="confirmed", root_cause="无证据"))

    def test_unknown_equipment_and_workorder(self):
        with self.assertRaises(NotFoundError):
            self.fx.svc.equipment_view("NOPE")
        with self.assertRaises(NotFoundError):
            self.fx.svc.work_order_view("WO-NOPE-001")


class PersistenceTests(unittest.TestCase):
    def test_open_work_orders_survive_restart(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "events.jsonl")
        fx = LifecycleFixture(path)
        fx.bootstrap()
        ev = fx.reading()
        rid = ev.payload["reading_id"]
        wo_id = fx.svc.report_fault(ReportFault(
            equipment_serial="SG-001", fault_code="E-RESTART", title="t",
            reporter="客服-陈", reading_ids=(rid,))).aggregate_id
        fx.svc.act(WorkOrderAction(
            wo_id, Action.TRANSFER, "客服-陈", Role.DISPATCH.value,
            to_team="diagnostic"))
        fx.svc.act(WorkOrderAction(
            wo_id, Action.DIAGNOSE, "诊断-赵", Role.DIAGNOSTIC.value,
            verdict="confirmed", root_cause="x", reading_ids=(rid,)))

        # 重启：重放日志，工单停在 diagnosed，未关闭、不丢失
        svc2 = LifecycleService(path)
        open_orders = svc2.list_open_work_orders()
        self.assertEqual([w["work_order_id"] for w in open_orders], [wo_id])
        self.assertEqual(open_orders[0]["stage"], "diagnosed")
        # 换件谱系也恢复
        svc2.replace_part(ReplacePart(
            "SG-001", "seal", old_part_serial="P-SEAL-1",
            new_part_serial="P-SEAL-2", operator="维修-孙"))
        self.assertEqual(
            svc2.equipment_view("SG-001")["current_topology"]["seal"], "P-SEAL-2")
        record = svc2.reconstruct_repair(wo_id)
        self.assertTrue(record["raw_readings"][0]["reading_id"].startswith("RD-"))
        self.assertEqual(len(record["raw_readings"][0]["digest"]), 64)
        tmp.cleanup()

    def test_tampered_log_is_detected(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "events.jsonl")
        fx = LifecycleFixture(path)
        fx.bootstrap()
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        event = json.loads(lines[0])
        event["payload"]["customer"] = "被篡改的客户"
        lines[0] = json.dumps(event, ensure_ascii=False) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        with self.assertRaises(LogIntegrityError):
            LifecycleService(path)
        tmp.cleanup()

    def test_stock_reserve_and_cancel_release(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "events.jsonl")
        fx = LifecycleFixture(path)
        fx.bootstrap()
        fx.svc.inbound_stock("P-GEAR-1", 2, "备件-钱")
        rid = fx.reading(sensor="g1").payload["reading_id"]
        wo_id = fx.svc.report_fault(ReportFault(
            equipment_serial="SG-001", fault_code="E-GEAR", title="t",
            reporter="客服-陈", reading_ids=(rid,))).aggregate_id
        fx.svc.act(WorkOrderAction(
            wo_id, Action.TRANSFER, "客服-陈", Role.DISPATCH.value,
            to_team="diagnostic"))
        fx.svc.act(WorkOrderAction(
            wo_id, Action.DIAGNOSE, "诊断-赵", Role.DIAGNOSTIC.value,
            verdict="confirmed", root_cause="齿轮磨损", reading_ids=(rid,)))
        fx.svc.act(WorkOrderAction(
            wo_id, Action.REQUEST_PARTS, "诊断-赵", Role.DIAGNOSTIC.value,
            required_parts=(("P-GEAR-1", "gear"),)))
        fx.svc.act(WorkOrderAction(
            wo_id, Action.RESERVE_PART, "备件-钱", Role.PARTS.value,
            part_serial="P-GEAR-1"))
        self.assertEqual(fx.svc.projection.stock["P-GEAR-1"], 1)
        # 主管取消工单，现货回补
        fx.svc.act(WorkOrderAction(
            wo_id, Action.CANCEL, "主管-周", Role.SUPERVISOR.value, note="客户停产"))
        self.assertEqual(fx.svc.projection.stock["P-GEAR-1"], 2)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
