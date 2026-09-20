"""基础健康契约 + 赛事协同接口的 HTTP 层测试。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ledger import Ledger
from service import Handler, SERVICE_ID, SERVICE_NAME, build_service, health_payload

REG = "2026-09-19T09:00:00+00:00"
TIP = "2026-09-20T12:00:00+00:00"
CRED = {"credential_type": "港澳通行签注", "doc_no": "E1",
        "valid_from": "2026-09-01T00:00:00+00:00",
        "valid_until": "2026-09-30T23:59:59+00:00"}
INS = {"person_id": "p1", "policy_no": "P1",
       "valid_from": "2026-09-01T00:00:00+00:00",
       "valid_until": "2026-09-30T23:59:59+00:00"}


def make_handler():
    """每个服务器实例使用独立台账，避免测试间状态串扰。"""
    service = build_service()

    class BoundHandler(Handler):
        pass

    BoundHandler.service = service
    return BoundHandler, service


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Handler, cls.service = make_handler()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _post(self, body):
        req = Request(f"{self.base_url}/api/commands",
                      data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def _get(self, path):
        try:
            with urlopen(f"{self.base_url}{path}", timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def _seed(self):
        self._post({"type": "register_admin", "actor": "s",
                    "payload": {"admin_id": "a1", "role": "association_admin"}})
        self._post({"type": "register_person", "actor": "s",
                    "payload": {"person_id": "p1", "name": "陳大文", "role": "player",
                                "team_id": "T-HK", "region": "HK", "jersey_number": 7,
                                "credential": CRED, "insurance": INS},
                    "occurred_at": REG})
        self._post({"type": "schedule_game", "actor": "s",
                    "payload": {"game_id": "G1", "home_team_id": "T-HK",
                                "away_team_id": "T-GD", "venue_id": "V1",
                                "tipoff_at": TIP},
                    "occurred_at": REG})
        self._post({"type": "publish_vote_roster", "actor": "a1",
                    "payload": {"game_id": "G1", "player_ids": ["p1"]},
                    "occurred_at": REG})

    def test_health_payload_has_stable_identity(self):
        self.assertEqual(health_payload(), {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME})

    def test_health_endpoint_returns_json(self):
        with urlopen(f"{self.base_url}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            self.assertEqual(json.load(response), health_payload())

    def test_unknown_route_is_not_exposed(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_command_and_roster_query_over_http(self):
        self._seed()
        status, body = self._get("/api/games/G1/roster?at=2026-09-20T11:00:00%2B00:00")
        self.assertEqual(status, 200)
        self.assertEqual([p["person_id"] for p in body["players"]], ["p1"])
        self.assertTrue(body["players"][0]["eligible"])

    def test_idempotent_reupload_reports_duplicate_once(self):
        self._seed()
        envelope = {"type": "verification_checkin", "actor": "DEV-1",
                    "payload": {"game_id": "G1", "person_id": "p1", "device_id": "DEV-1"},
                    "occurred_at": "2026-09-20T11:55:00+00:00",
                    "idempotency_key": "REG-HTTP-1"}
        status1, body1 = self._post(envelope)
        status2, body2 = self._post(envelope)
        self.assertEqual(status1, 201)
        self.assertEqual(status2, 200)
        self.assertEqual(body2["status"], "duplicate")
        self.assertEqual(body2["event"]["event_id"], body1["event"]["event_id"])
        status, journal = self._get("/api/games/G1/verifications")
        self.assertEqual(status, 200)
        self.assertEqual(journal["count"], 1)

    def test_domain_error_returns_422(self):
        status, body = self._post({"type": "register_person", "actor": "s",
                                   "payload": {"person_id": "px", "name": "x"}})
        self.assertEqual(status, 422)
        self.assertIn("缺少字段", body["error"])

    def test_minimum_necessary_view_denies_cross_region(self):
        self._seed()
        self._post({"type": "register_admin", "actor": "s",
                    "payload": {"admin_id": "a-gd", "role": "region_admin",
                                "regions": ["GD"]}})
        status, body = self._get("/api/persons/p1?viewer=a-gd")
        self.assertEqual(status, 422)
        self.assertIn("跨地区查阅被拒绝", body["error"])
        status, body = self._get("/api/persons/p1?viewer=a1")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["credential"]["doc_no"], "E1")

    def test_record_versions_endpoint(self):
        self._seed()
        self._post({"type": "submit_referee_record", "actor": "ref-1",
                    "payload": {"record_id": "R1", "game_id": "G1",
                                "snapshot": {"home_score": 80}}})
        status, body = self._get("/api/records/R1/versions")
        self.assertEqual(status, 200)
        self.assertEqual(body["current_version"], 1)
        self.assertEqual(body["versions"][0]["snapshot"]["home_score"], 80)


if __name__ == "__main__":
    unittest.main()
