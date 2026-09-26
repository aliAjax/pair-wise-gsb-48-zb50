import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app import build_service, build_settlement_service
from src.http_api import create_server


BASE_DIR = Path(__file__).resolve().parent.parent


class SettlementHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db_path = str(Path(self.temp.name) / "http.db")
        service = build_service(db_path)
        settlement = build_settlement_service(db_path)
        self.server = create_server("127.0.0.1", 0, service, BASE_DIR / "static", settlement)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, method, path, payload=None, role="settlement_officer", user="officer"):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request("http://127.0.0.1:%s%s" % (self.port, path), data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("X-User-Id", user)
        request.add_header("X-Role", role)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_http_flow(self):
        status, body = self.request("POST", "/api/settlements", {"reference": "SET-WEB-1", "data": {"required_quantity": 1000, "required_amount": 10000.0}})
        self.assertEqual(status, 201)
        sid = body["id"]

        status, body = self.request("POST", "/api/settlements/%s/entries" % sid, {"data": {"quantity": 600, "amount": 6000.0, "source_ref": "WEB-A", "operator": "张三"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["remaining_quantity"], 400)

        # 超额整笔退回 -> 422
        status, body = self.request("POST", "/api/settlements/%s/entries" % sid, {"data": {"quantity": 500, "amount": 1000.0, "source_ref": "WEB-B", "operator": "李四"}})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "validation_error")

        # 重复来源单号 -> 409
        status, body = self.request("POST", "/api/settlements/%s/entries" % sid, {"data": {"quantity": 10, "amount": 10.0, "source_ref": "WEB-A", "operator": "李四"}})
        self.assertEqual(status, 409)

        # 结清
        status, body = self.request("POST", "/api/settlements/%s/entries" % sid, {"data": {"quantity": 400, "amount": 4000.0, "source_ref": "WEB-C", "operator": "王五"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "settled")

        # 已结清不再接收 -> 409
        status, _ = self.request("POST", "/api/settlements/%s/entries" % sid, {"data": {"quantity": 1, "amount": 1.0, "source_ref": "WEB-D", "operator": "赵六"}})
        self.assertEqual(status, 409)

        # 明细列表
        status, body = self.request("GET", "/api/settlements/%s/entries" % sid)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 2)

        # 冲正（已结清：保留全部到账，无退回明细）
        status, body = self.request("POST", "/api/settlements/%s/reverse" % sid, {"data": {"reverse_reason": "网页端冲正"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "reversed")
        self.assertEqual(len([e for e in body["entries"] if e["entry_type"] == "arrival"]), 2)

        # 权限拒绝
        status, body = self.request("GET", "/api/settlements", None, role="trader", user="t")
        self.assertEqual(status, 403)

        # 页面可达
        request = urllib.request.Request("http://127.0.0.1:%s/settlement" % self.port)
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("大额交收".encode("utf-8"), response.read())


if __name__ == "__main__":
    unittest.main()
