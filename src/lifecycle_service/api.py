"""HTTP 接口（标准库实现，零第三方依赖）。

启动：
    python -m lifecycle_service.api --log data/events.jsonl --port 8080

错误到状态码的映射：
    ValidationFailure/BackfillRejectedError -> 400
    PermissionDeniedError                  -> 403
    NotFoundError                          -> 404
    ConflictError/DuplicateWorkOrderError  -> 409（重复工单响应体带既有工单）
    LogIntegrityError                      -> 500（启动时也会直接失败）
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .errors import (
    BackfillRejectedError,
    ConflictError,
    DomainError,
    DuplicateWorkOrderError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailure,
)
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


class ApiHandler(BaseHTTPRequestHandler):
    service: LifecycleService  # 由工厂注入

    # -- 工具 --------------------------------------------------------------

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationFailure(f"请求体不是合法 JSON：{exc}")
        if not isinstance(data, dict):
            raise ValidationFailure("请求体必须是 JSON 对象")
        return data

    def log_message(self, fmt, *args):  # 保持 stderr 简洁
        return

    # -- 路由 --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._json(200, {"status": "ok",
                                 "events": self.service.projection.head_seq})
            elif path == "/work-orders":
                self._json(200, {"work_orders": self.service.list_open_work_orders()})
            elif path.startswith("/work-orders/") and path.endswith("/reconstruction"):
                wo_id = path.split("/")[2]
                self._json(200, self.service.reconstruct_repair(wo_id))
            elif path.startswith("/work-orders/"):
                wo_id = path.split("/")[2]
                self._json(200, self.service.work_order_view(wo_id))
            elif path.startswith("/equipment/"):
                serial = path.split("/")[2]
                self._json(200, self.service.equipment_view(serial))
            else:
                self._json(404, {"error": "not_found", "message": f"未知路径：{path}"})
        except DomainError as exc:
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path.rstrip("/")
            body = self._body()
            if path == "/equipment":
                event = self.service.register_equipment(RegisterEquipment(
                    serial=body["serial"], model=body.get("model", ""),
                    customer=body.get("customer", ""),
                    delivered_at=body.get("delivered_at", ""),
                    operator=body.get("operator", "api"),
                    bill_of_materials=body.get("bill_of_materials", {}),
                    warranty_months=int(body.get("warranty_months", 12)),
                    occurred_at=body.get("occurred_at")))
                self._json(201, {"event_id": event.event_id, "seq": event.seq})
            elif path.startswith("/equipment/") and path.endswith("/install"):
                serial = path.split("/")[2]
                event = self.service.install_equipment(InstallEquipment(
                    serial=serial, site=body.get("site", ""),
                    operator=body.get("operator", "api"),
                    occurred_at=body.get("occurred_at")))
                self._json(201, {"event_id": event.event_id, "seq": event.seq})
            elif path.startswith("/equipment/") and path.endswith("/parts/replace"):
                serial = path.split("/")[2]
                event = self.service.replace_part(ReplacePart(
                    equipment_serial=serial, slot=body["slot"],
                    old_part_serial=body.get("old_part_serial"),
                    new_part_serial=body["new_part_serial"],
                    operator=body.get("operator", "api"),
                    work_order_id=body.get("work_order_id"),
                    occurred_at=body.get("occurred_at"),
                    backfill=bool(body.get("backfill", False)),
                    note=body.get("note", "")))
                self._json(201, {"event_id": event.event_id, "seq": event.seq})
            elif path.startswith("/equipment/") and path.endswith("/warranty"):
                serial = path.split("/")[2]
                event = self.service.change_warranty(ChangeWarranty(
                    equipment_serial=serial, new_coverage=body["new_coverage"],
                    operator=body.get("operator", "api"), reason=body.get("reason", ""),
                    warranty_end=body.get("warranty_end"),
                    occurred_at=body.get("occurred_at")))
                self._json(201, {"event_id": event.event_id, "seq": event.seq})
            elif path == "/readings":
                event = self.service.ingest_reading(IngestReading(
                    equipment_serial=body["equipment_serial"], sensor=body["sensor"],
                    observed_at=body["observed_at"], values=body.get("values", {}),
                    source_uri=body.get("source_uri", ""),
                    recorded_at=body.get("recorded_at"),
                    backfill=bool(body.get("backfill", False))))
                self._json(201, {"event_id": event.event_id, "seq": event.seq,
                                 "reading_id": event.payload["reading_id"]})
            elif path == "/stock/inbound":
                event = self.service.inbound_stock(
                    body["part_serial"], int(body.get("quantity", 1)),
                    body.get("operator", "api"), body.get("occurred_at"))
                self._json(201, {"event_id": event.event_id, "seq": event.seq})
            elif path == "/work-orders":
                cmd = ReportFault(
                    equipment_serial=body["equipment_serial"],
                    fault_code=body["fault_code"], title=body.get("title", ""),
                    reporter=body.get("reporter", "api"), role=body.get("role", "dispatch"),
                    severity=body.get("severity", "medium"),
                    occurred_at=body.get("occurred_at"),
                    reading_ids=tuple(body.get("reading_ids", [])),
                    description=body.get("description", ""),
                    backfill=bool(body.get("backfill", False)),
                    idempotency_key=body.get("idempotency_key"))
                try:
                    event = self.service.report_fault(cmd)
                except DuplicateWorkOrderError as exc:
                    self._json(409, {"error": "duplicate_work_order",
                                     "message": str(exc),
                                     "existing_work_order_id": exc.existing_work_order_id})
                    return
                self._json(201, {"event_id": event.event_id, "seq": event.seq,
                                 "work_order_id": event.aggregate_id})
            elif path.startswith("/work-orders/") and path.endswith("/actions"):
                wo_id = path.split("/")[2]
                event = self.service.act(WorkOrderAction(
                    work_order_id=wo_id, action=body["action"],
                    operator=body.get("operator", "api"), role=body.get("role", "dispatch"),
                    occurred_at=body.get("occurred_at"),
                    backfill=bool(body.get("backfill", False)),
                    to_team=body.get("to_team"), verdict=body.get("verdict"),
                    root_cause=body.get("root_cause", ""),
                    required_parts=tuple(tuple(x) for x in body.get("required_parts", [])),
                    reading_ids=tuple(body.get("reading_ids", [])),
                    part_serial=body.get("part_serial"), eta=body.get("eta"),
                    resolution=body.get("resolution", ""), note=body.get("note", ""),
                    target_stage=body.get("target_stage"),
                    idempotency_key=body.get("idempotency_key")))
                self._json(201, {"event_id": event.event_id, "seq": event.seq,
                                 "stage_after": self.service.work_order_view(
                                     wo_id)["stage"]})
            else:
                self._json(404, {"error": "not_found", "message": f"未知路径：{path}"})
        except DomainError as exc:
            self._error(exc)
        except KeyError as exc:
            self._json(400, {"error": "validation", "message": f"缺少必填字段：{exc.args[0]}"})

    def _error(self, exc: DomainError) -> None:
        if isinstance(exc, NotFoundError):
            status, code = 404, "not_found"
        elif isinstance(exc, PermissionDeniedError):
            status, code = 403, "permission_denied"
        elif isinstance(exc, (ConflictError, DuplicateWorkOrderError)):
            status, code = 409, "conflict"
        elif isinstance(exc, (ValidationFailure, BackfillRejectedError)):
            status, code = 400, "validation"
        else:
            status, code = 400, "domain_error"
        body = {"error": code, "message": str(exc)}
        if isinstance(exc, DuplicateWorkOrderError):
            body["existing_work_order_id"] = exc.existing_work_order_id
        self._json(status, body)


def build_server(log_path: str, port: int = 8080, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    service = LifecycleService(log_path)

    class _Handler(ApiHandler):
        pass

    _Handler.service = service
    server = ThreadingHTTPServer((host, port), _Handler)
    server.service = service  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="设备生命周期服务 HTTP 接口")
    parser.add_argument("--log", default="data/events.jsonl", help="事件日志路径")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    server = build_server(args.log, args.port, args.host)
    print(f"生命周期服务监听 http://{args.host}:{args.port}（事件日志 {args.log}）",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
