"""命令行工具：客服可在无 HTTP 环境时登记事件并重建维修经过。

示例：
    python -m lifecycle_service.cli --log data/events.jsonl reconstruct WO-SG-001-001
    python -m lifecycle_service.cli --log data/events.jsonl list-open
    python -m lifecycle_service.cli --log data/events.jsonl act WO-SG-001-001 \\
        --action diagnose --role diagnostic --operator zhang --verdict repair_scope \\
        --reading-ids RD-abcd1234
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import DomainError, DuplicateWorkOrderError
from .models import (
    ChangeWarranty,
    IngestReading,
    InstallEquipment,
    RegisterEquipment,
    ReplacePart,
    ReportFault,
    WorkOrderAction,
)
from .service import LifecycleService


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lifecycle-cli", description="设备生命周期服务命令行")
    p.add_argument("--log", default="data/events.jsonl", help="事件日志路径")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("register", help="设备交付登记")
    sp.add_argument("serial")
    sp.add_argument("--model", required=True)
    sp.add_argument("--customer", required=True)
    sp.add_argument("--delivered-at", required=True)
    sp.add_argument("--operator", required=True)
    sp.add_argument("--bom", help="JSON：槽位到部件序列号映射", default="{}")
    sp.add_argument("--warranty-months", type=int, default=12)
    sp.add_argument("--occurred-at")

    sp = sub.add_parser("install", help="设备安装")
    sp.add_argument("serial")
    sp.add_argument("--site", required=True)
    sp.add_argument("--operator", required=True)
    sp.add_argument("--occurred-at")

    sp = sub.add_parser("replace-part", help="部件更换（维护拓扑）")
    sp.add_argument("serial")
    sp.add_argument("--slot", required=True)
    sp.add_argument("--old-part")
    sp.add_argument("--new-part", required=True)
    sp.add_argument("--operator", required=True)
    sp.add_argument("--work-order")
    sp.add_argument("--occurred-at")
    sp.add_argument("--backfill", action="store_true")
    sp.add_argument("--note", default="")

    sp = sub.add_parser("warranty", help="保修范围变化")
    sp.add_argument("serial")
    sp.add_argument("--coverage", required=True,
                    choices=["warranty", "service_contract", "out_of_warranty", "goodwill"])
    sp.add_argument("--reason", required=True)
    sp.add_argument("--operator", required=True)
    sp.add_argument("--warranty-end")
    sp.add_argument("--occurred-at")

    sp = sub.add_parser("reading", help="接入传感读数")
    sp.add_argument("serial")
    sp.add_argument("--sensor", required=True)
    sp.add_argument("--observed-at", required=True)
    sp.add_argument("--values", required=True, help="JSON：测点到数值映射")
    sp.add_argument("--source-uri", default="")
    sp.add_argument("--recorded-at")
    sp.add_argument("--backfill", action="store_true")

    sp = sub.add_parser("stock-in", help="备件现货入库")
    sp.add_argument("part_serial")
    sp.add_argument("--quantity", type=int, default=1)
    sp.add_argument("--operator", required=True)

    sp = sub.add_parser("report", help="受理报修")
    sp.add_argument("serial")
    sp.add_argument("--fault-code", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--reporter", required=True)
    sp.add_argument("--role", default="dispatch")
    sp.add_argument("--severity", default="medium")
    sp.add_argument("--occurred-at")
    sp.add_argument("--reading-ids", nargs="*", default=[])
    sp.add_argument("--description", default="")
    sp.add_argument("--backfill", action="store_true")
    sp.add_argument("--idempotency-key")

    sp = sub.add_parser("act", help="工单处置动作")
    sp.add_argument("work_order")
    sp.add_argument("--action", required=True)
    sp.add_argument("--operator", required=True)
    sp.add_argument("--role", required=True)
    sp.add_argument("--occurred-at")
    sp.add_argument("--backfill", action="store_true")
    sp.add_argument("--to-team")
    sp.add_argument("--verdict")
    sp.add_argument("--root-cause", default="")
    sp.add_argument("--required-parts", nargs="*", default=[],
                    help="形如 部件序列号:槽位，可多个")
    sp.add_argument("--reading-ids", nargs="*", default=[])
    sp.add_argument("--part-serial")
    sp.add_argument("--eta")
    sp.add_argument("--resolution", default="")
    sp.add_argument("--note", default="")
    sp.add_argument("--target-stage")
    sp.add_argument("--idempotency-key")

    sp = sub.add_parser("show-equipment", help="查看设备视图（拓扑/保修/谱系）")
    sp.add_argument("serial")

    sp = sub.add_parser("show-workorder", help="查看工单")
    sp.add_argument("work_order")

    sp = sub.add_parser("list-open", help="列出未关闭工单")

    sp = sub.add_parser("reconstruct", help="重建一次维修的完整经过")
    sp.add_argument("work_order")
    sp.add_argument("--json", action="store_true", help="输出结构化 JSON")

    return p


def _required_parts(items: list[str]):
    result = []
    for item in items:
        if ":" not in item:
            raise SystemExit(f"备件项格式应为 部件:槽位，收到：{item}")
        serial, slot = item.split(":", 1)
        result.append((serial, slot))
    return tuple(result)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    service = LifecycleService(args.log)
    try:
        return _dispatch(service, args)
    except DuplicateWorkOrderError as exc:
        print(f"[重复工单] {exc}；既有工单：{exc.existing_work_order_id}", file=sys.stderr)
        return 2
    except DomainError as exc:
        print(f"[被拒绝] {exc}", file=sys.stderr)
        return 1


def _dispatch(service: LifecycleService, args) -> int:
    cmd = args.command
    if cmd == "register":
        e = service.register_equipment(RegisterEquipment(
            serial=args.serial, model=args.model, customer=args.customer,
            delivered_at=args.delivered_at, operator=args.operator,
            bill_of_materials=json.loads(args.bom),
            warranty_months=args.warranty_months, occurred_at=args.occurred_at))
        _print({"event_id": e.event_id, "seq": e.seq})
    elif cmd == "install":
        e = service.install_equipment(InstallEquipment(
            serial=args.serial, site=args.site, operator=args.operator,
            occurred_at=args.occurred_at))
        _print({"event_id": e.event_id, "seq": e.seq})
    elif cmd == "replace-part":
        e = service.replace_part(ReplacePart(
            equipment_serial=args.serial, slot=args.slot,
            old_part_serial=args.old_part, new_part_serial=args.new_part,
            operator=args.operator, work_order_id=args.work_order,
            occurred_at=args.occurred_at, backfill=args.backfill, note=args.note))
        _print({"event_id": e.event_id, "seq": e.seq})
    elif cmd == "warranty":
        e = service.change_warranty(ChangeWarranty(
            equipment_serial=args.serial, new_coverage=args.coverage,
            operator=args.operator, reason=args.reason,
            warranty_end=args.warranty_end, occurred_at=args.occurred_at))
        _print({"event_id": e.event_id, "seq": e.seq})
    elif cmd == "reading":
        e = service.ingest_reading(IngestReading(
            equipment_serial=args.serial, sensor=args.sensor,
            observed_at=args.observed_at, values=json.loads(args.values),
            source_uri=args.source_uri, recorded_at=args.recorded_at,
            backfill=args.backfill))
        _print({"event_id": e.event_id, "seq": e.seq,
                "reading_id": e.payload["reading_id"]})
    elif cmd == "stock-in":
        e = service.inbound_stock(args.part_serial, args.quantity, args.operator)
        _print({"event_id": e.event_id, "seq": e.seq})
    elif cmd == "report":
        e = service.report_fault(ReportFault(
            equipment_serial=args.serial, fault_code=args.fault_code,
            title=args.title, reporter=args.reporter, role=args.role,
            severity=args.severity, occurred_at=args.occurred_at,
            reading_ids=tuple(args.reading_ids), description=args.description,
            backfill=args.backfill, idempotency_key=args.idempotency_key))
        _print({"event_id": e.event_id, "seq": e.seq, "work_order_id": e.aggregate_id})
    elif cmd == "act":
        e = service.act(WorkOrderAction(
            work_order_id=args.work_order, action=args.action,
            operator=args.operator, role=args.role, occurred_at=args.occurred_at,
            backfill=args.backfill, to_team=args.to_team, verdict=args.verdict,
            root_cause=args.root_cause,
            required_parts=_required_parts(args.required_parts),
            reading_ids=tuple(args.reading_ids), part_serial=args.part_serial,
            eta=args.eta, resolution=args.resolution, note=args.note,
            target_stage=args.target_stage, idempotency_key=args.idempotency_key))
        _print({"event_id": e.event_id, "seq": e.seq})
    elif cmd == "show-equipment":
        _print(service.equipment_view(args.serial))
    elif cmd == "show-workorder":
        _print(service.work_order_view(args.work_order))
    elif cmd == "list-open":
        _print({"work_orders": service.list_open_work_orders()})
    elif cmd == "reconstruct":
        data = service.reconstruct_repair(args.work_order)
        if args.json:
            _print(data)
        else:
            _render_reconstruction(data)
    return 0


def _render_reconstruction(data: dict) -> None:
    wo = data["work_order"]
    eq = data["equipment"]
    ver = data["equipment_version_at_fault"]
    cov = data["coverage_at_fault"]
    print("=" * 72)
    print(f"维修经过重建：{wo['work_order_id']}  {wo['fault_code']} {wo['title']}")
    print("=" * 72)
    print(f"设备：{eq['serial']}（{eq['model']}，客户 {eq['customer']}，现场 {eq['site']}）")
    print(f"阶段：{wo['stage']}    当前团队：{wo['team']}    严重度：{wo['severity']}")
    print(f"故障发生：{wo['occurred_at']}    系统受理：{wo['recorded_at']}"
          + ("    [离线补录]" if wo["backfill"] else ""))
    print(f"故障时配置版本：v{ver['config_version']}"
          f"（当前 v{ver['current_config_version']}）")
    print("故障时拓扑：")
    for slot, part in sorted(ver["topology"].items()):
        print(f"  - {slot}: {part}")
    print(f"费用归属：{cov['coverage']}（依据：{cov['basis']}）")
    if data["raw_readings"]:
        print("-" * 72)
        print("原始传感证据（只读、可回溯）：")
        for r in data["raw_readings"]:
            flag = " [补录]" if r["backfill"] else ""
            print(f"  - {r['reading_id']} 传感器 {r['sensor']} 观测于 {r['observed_at']}{flag}")
            print(f"      数值={json.dumps(r['values'], ensure_ascii=False)}")
            print(f"      来源={r['source_uri'] or '-'} 摘要={r['digest'][:20]}…")
    print("-" * 72)
    print("处置时间线：")
    for item in data["timeline"]:
        flag = " 补录" if item["backfill"] else ""
        print(f"  #{item['seq']:<3} {item['occurred_at']} "
              f"[{item['stage_after']}]{flag} {item['operator']}：{item['title']}")
    if wo["diagnosis"]:
        print("-" * 72)
        d = wo["diagnosis"]
        print(f"诊断结论：{d['verdict']}；根因：{d['root_cause'] or '-'}")
        print(f"  依据读数：{', '.join(d['reading_ids'])}")
    if wo["coverage_decision"]:
        cd = wo["coverage_decision"]
        print(f"结算：{cd['coverage']}（{cd['basis']}）")
    chain = data["evidence_chain"]
    print("=" * 72)
    print(f"证据链：事件 #{chain['first_seq']}..#{chain['last_seq']}，"
          f"共 {chain['event_count']} 条；日志尾序号 #{chain['log_tail_seq']}")


if __name__ == "__main__":
    raise SystemExit(main())
