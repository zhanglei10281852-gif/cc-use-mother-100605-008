"""HTTP 接口测试：服务在进程内启动，用 urllib 打真实端口。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from lifecycle_service.api import build_server

NOW = datetime.now(timezone.utc)


def http(method: str, url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "events.jsonl")
        self.server = build_server(self.path, port=0)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        http("POST", f"{self.base}/equipment", {
            "serial": "SG-9", "model": "M-1", "customer": "cust",
            "delivered_at": (NOW - timedelta(days=30)).isoformat(),
            "operator": "li", "bill_of_materials": {"seal": "S-1"}})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def test_health_and_crud_flow(self):
        status, body = http("GET", f"{self.base}/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["events"], 1)

        status, body = http("POST", f"{self.base}/equipment/SG-9/install",
                            {"site": "A区", "operator": "wang"})
        self.assertEqual(status, 201)

        status, body = http("POST", f"{self.base}/readings", {
            "equipment_serial": "SG-9", "sensor": "v",
            "observed_at": (NOW - timedelta(hours=2)).isoformat(),
            "values": {"rms": 5.0}})
        self.assertEqual(status, 201)
        rid = body["reading_id"]

        status, body = http("POST", f"{self.base}/work-orders", {
            "equipment_serial": "SG-9", "fault_code": "F1", "title": "故障",
            "reporter": "cs", "reading_ids": [rid]})
        self.assertEqual(status, 201)
        wo = body["work_order_id"]

        # 重复 → 409 并指回既有工单
        status, body = http("POST", f"{self.base}/work-orders", {
            "equipment_serial": "SG-9", "fault_code": "F1", "title": "重复",
            "reporter": "cs"})
        self.assertEqual(status, 409)
        self.assertEqual(body["existing_work_order_id"], wo)

        # 未关闭列表
        status, body = http("GET", f"{self.base}/work-orders")
        self.assertEqual([w["work_order_id"] for w in body["work_orders"]], [wo])

        # 设备视图
        status, body = http("GET", f"{self.base}/equipment/SG-9")
        self.assertEqual(body["current_topology"]["seal"], "S-1")

        # 重建
        status, body = http("GET", f"{self.base}/work-orders/{wo}/reconstruction")
        self.assertEqual(status, 200)
        self.assertEqual(body["raw_readings"][0]["reading_id"], rid)

    def test_error_status_mapping(self):
        status, _ = http("GET", f"{self.base}/equipment/NOPE")
        self.assertEqual(status, 404)
        status, body = http("POST", f"{self.base}/equipment", {"serial": ""})
        self.assertEqual(status, 400)
        # 非法 JSON
        req = urllib.request.Request(
            f"{self.base}/work-orders", data=b"{not json", method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            self.fail("应失败")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
