"""HTTP 端到端冒烟：真实端口、真实 JSON 请求，覆盖主链路与鉴权头。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from baysports.api import App, Handler, make_server
from baysports.clock import FixedClock

TIPOFF = "2026-09-20T12:00:00Z"


class ApiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = App(clock=FixedClock("2026-09-19T09:00:00Z"))
        cls.server = make_server("127.0.0.1", 0, cls.app)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, payload=None, *, staff=None, role=None,
             region=None):
        headers = {"Content-Type": "application/json"}
        if staff:
            headers["X-Staff-Id"] = staff
        if role:
            headers["X-Role"] = role
        if region:
            headers["X-Region"] = region
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = Request(self.base + path, data=data if method == "POST" else None,
                          headers=headers, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_full_flow_over_http(self):
        # 健康检查
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "bay-sports")

        # 未知路径仍为 404
        status, _ = self.call("GET", "/unknown")
        self.assertEqual(status, 404)

        # 注册
        self.assertEqual(self.call("POST", "/teams", {
            "team_id": "T-HK", "name": "香港湾区队", "region": "HK"},
            staff="sec-hk-01")[0], 201)
        self.assertEqual(self.call("POST", "/persons", {
            "person_id": "P1", "team_id": "T-HK", "name": "港员甲",
            "region": "HK", "role": "player", "jersey_no": "7",
            "languages": ["zh-HK", "en"]}, staff="sec-hk-01")[0], 201)
        self.assertEqual(self.call("POST", "/persons/P1/travel-document", {
            "doc_type": "港澳通行证", "number": "H12345678",
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_until": "2026-09-25T23:59:00Z"}, staff="sec-hk-01")[0], 201)
        self.assertEqual(self.call("POST", "/persons/P1/insurance", {
            "policy_no": "POL1", "insurer": "联保",
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_until": "2026-09-22T23:59:00Z", "coverage": {}},
            staff="sec-hk-01")[0], 201)
        self.assertEqual(self.call("POST", "/persons/P1/eligibility", {
            "type": "asg_participation",
            "valid_from": "2026-09-19T00:00:00Z",
            "valid_until": "2026-09-21T23:59:00Z", "basis": {}},
            staff="sec-chief-01")[0], 201)

        # 票选确认
        self.call("POST", "/ballot/nominations",
                  {"person_id": "P1", "votes": 9000}, staff="ballot")
        self.assertEqual(self.call("POST", "/ballot/confirm",
                                   {"person_ids": ["P1"]},
                                   staff="sec-chief-01")[0], 201)

        # 场馆与比赛
        self.assertEqual(self.call("POST", "/venue-slots", {
            "slot_id": "S1", "venue": "湾区体育馆", "region": "GD",
            "start": "2026-09-20T09:00:00Z", "end": "2026-09-20T15:00:00Z",
            "purpose": "正赛", "capacity": 100}, staff="venue-gd")[0], 201)
        self.assertEqual(self.call("POST", "/games", {
            "game_id": "G1", "label": "正赛", "slot_id": "S1",
            "tipoff": TIPOFF, "team_ids": ["T-HK"]},
            staff="sec-chief-01")[0], 201)
        self.assertEqual(self.call("POST", "/games/G1/roster", {
            "player_ids": ["P1"], "staff_ids": [],
            "at": "2026-09-20T10:00:00Z"}, staff="sec-hk-01")[0], 201)

        # 核验：设备失联补传同编号
        self.app.clock.set("2026-09-20T11:40:00Z")
        status1, body1 = self.call("POST", "/verifications", {
            "registration_no": "VR-1", "game_id": "G1", "person_id": "P1",
            "device_id": "GATE-1", "result": "pass",
            "occurred_at": "2026-09-20T11:30:00Z"}, staff="marshal")
        self.assertEqual(status1, 201)
        self.assertTrue(body1["event"]["data"]["backfill"])
        status2, body2 = self.call("POST", "/verifications", {
            "registration_no": "VR-1", "game_id": "G1", "person_id": "P1",
            "device_id": "GATE-1", "result": "pass",
            "occurred_at": "2026-09-20T11:30:00Z"}, staff="marshal")
        self.assertTrue(body2["replayed"])
        self.assertEqual(body1["event"]["seq"], body2["event"]["seq"])

        # 裁判签字 + 技术统计修正新版本
        self.app.clock.set("2026-09-20T14:00:00Z")
        self.assertEqual(self.call("POST", "/games/G1/referee-records", {
            "payload": {"score": {"HK": 101}}, "occurred_at":
            "2026-09-20T13:55:00Z"}, staff="ref-1")[0], 201)
        status, body = self.call("POST", "/games/G1/referee-records", {
            "payload": {"score": {"HK": 100}},
            "occurred_at": "2026-09-20T14:00:00Z"}, staff="ref-1")
        self.assertEqual(status, 409)

        self.assertEqual(self.call("POST", "/games/G1/referee-records/amend", {
            "payload": {"score": {"HK": 101}, "assists": 9},
            "amendment_type": "stat_correction", "reason": "加时统计更正",
            "occurred_at": "2026-09-20T15:00:00Z",
            "basis": {"clause": "3.2"}}, staff="jury-1")[0], 201)

        status, body = self.call("GET", "/games/G1/referee-records",
                                 staff="sec-chief-01", role="secretariat_chief")
        self.assertEqual(body["effective_version"], 2)
        self.assertEqual(body["versions"][0]["payload"]["score"], {"HK": 101})

        # 权限：广东场地员查香港球员被拒（行级）
        status, body = self.call("GET", "/persons/P1?purpose=venue_check",
                                 staff="venue-gd-01", role="venue_marshal",
                                 region="GD")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")

        # 权限：本地区场地员可查但无证件字段（列级）
        status, body = self.call("GET", "/persons/P1?purpose=venue_check",
                                 staff="venue-hk-01", role="venue_marshal",
                                 region="HK")
        self.assertEqual(status, 200)
        self.assertNotIn("travel_document", body["person"])
        self.assertIn("eligible_now", body["person"])

        # 无身份读取被拒
        status, _ = self.call("GET", "/games/G1/roster")
        self.assertEqual(status, 403)

        # 事件全量日志仅审计岗
        status, _ = self.call("GET", "/events", staff="sec-hk-01",
                              role="secretariat")
        self.assertEqual(status, 403)
        status, body = self.call("GET", "/events", staff="audit-01",
                                 role="auditor", region="GD")
        self.assertEqual(status, 200)
        self.assertGreater(body["version"], 10)

    def test_bad_json_is_400(self):
        request = Request(self.base + "/teams", data=b"{not json",
                          headers={"Content-Type": "application/json",
                                   "X-Staff-Id": "x"}, method="POST")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(request, timeout=3)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
