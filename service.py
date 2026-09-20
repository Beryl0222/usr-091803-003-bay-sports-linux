"""湾区赛事协同台账的运行入口与 HTTP 接口。

- GET  /health                                  服务身份健康检查
- POST /api/commands                            统一写入入口（所有领域命令）
- GET  /api/games/<id>/roster?at=               当时有效的当场名单
- GET  /api/games/<id>/roster-history           名单变化批准链（谁、依据什么）
- GET  /api/games/<id>/postmortem?at=           赛后一站式归档
- GET  /api/games/<id>/medical?at=              医疗处置
- GET  /api/games/<id>/expenses?at=             费用分摊
- GET  /api/games/<id>/score                    比分与申诉/修正链
- GET  /api/games/<id>/verifications            现场核验登记（含补传标记）
- GET  /api/persons/<id>/eligibility?game_id=&at=
- GET  /api/persons/<id>?viewer=<admin_id>&at=  按职责脱敏的个人资料
- GET  /api/notices/<id>                        各版通知送达情况
- GET  /api/records/<id>/versions               裁判记录原始件与修正版本链

所有时间参数 at 为 ISO8601；写入命令体：
{"type": "<命令类型>", "actor": "<操作人>", "payload": {...},
 "occurred_at": "<可选，离线补传的实际发生时间>",
 "idempotency_key": "<可选，设备登记号>"}
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ledger import IdempotentReplay, Ledger, now_ts
from tournament import DomainError, TournamentService

SERVICE_ID = "bay-sports"
SERVICE_NAME = "湾区赛事协同台账"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_service(store_path: str | None = None) -> TournamentService:
    return TournamentService(Ledger(store_path))


class Handler(BaseHTTPRequestHandler):
    """健康检查 + 赛事协同只读/只追加接口。service 可由测试注入。"""

    service: TournamentService = build_service()

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/health":
            self._send_json(200, health_payload())
            return
        try:
            self._route_get(path, query)
        except DomainError as error:
            self._send_json(422, {"error": str(error)})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/commands":
            self.send_error(404)
            return
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw.decode("utf-8"))
            event = self.service.command(
                body["type"], body["actor"], body.get("payload", {}),
                occurred_at=body.get("occurred_at"),
                idempotency_key=body.get("idempotency_key"),
            )
            self._send_json(201, {"status": "accepted", "event": event})
        except (KeyError, json.JSONDecodeError) as error:
            self._send_json(400, {"error": f"请求体不合法: {error}"})
        except IdempotentReplay as replay:
            self._send_json(200, {"status": "duplicate",
                                  "event": replay.original.to_dict()})
        except DomainError as error:
            self._send_json(422, {"error": str(error)})

    def _route_get(self, path, query):
        def q(name, default=None):
            return query.get(name, [default])[0]

        svc = self.service
        parts = [x for x in path.split("/") if x]
        if path.startswith("/api/games/") and path.endswith("/roster"):
            self._send_json(200, svc.effective_roster(parts[2], q("at")))
        elif path.startswith("/api/games/") and path.endswith("/roster-history"):
            self._send_json(200, svc.roster_change_history(parts[2]))
        elif path.startswith("/api/games/") and path.endswith("/postmortem"):
            self._send_json(200, svc.game_postmortem(parts[2], q("at")))
        elif path.startswith("/api/games/") and path.endswith("/medical"):
            self._send_json(200, svc.medical_view(parts[2], q("at")))
        elif path.startswith("/api/games/") and path.endswith("/expenses"):
            self._send_json(200, svc.expenses(parts[2], q("at")))
        elif path.startswith("/api/games/") and path.endswith("/score"):
            self._send_json(200, svc.game_score_history(parts[2]))
        elif path.startswith("/api/games/") and path.endswith("/verifications"):
            self._send_json(200, svc.verification_journal(parts[2]))
        elif path.startswith("/api/persons/") and path.endswith("/eligibility"):
            game_id = q("game_id")
            if not game_id:
                self._send_json(400, {"error": "缺少 game_id"})
                return
            self._send_json(200, svc.explain_eligibility(parts[2], game_id,
                                                         q("at") or now_ts()))
        elif path.startswith("/api/persons/") and len(parts) == 3:
            viewer = q("viewer")
            if not viewer:
                self._send_json(400, {"error": "缺少 viewer（管理员 ID）"})
                return
            self._send_json(200, svc.person_view(viewer, parts[2], q("at")))
        elif path.startswith("/api/notices/") and len(parts) == 3:
            self._send_json(200, svc.notice_status(parts[2]))
        elif path.startswith("/api/records/") and path.endswith("/versions"):
            self._send_json(200, svc.referee_record_versions(parts[2]))
        else:
            self.send_error(404)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--store", default=None, help="事件台账 JSON 落盘路径")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        svc = build_service(args.store)
        assert isinstance(svc, TournamentService)
        print("基础检查通过")
        return
    Handler.service = build_service(args.store)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
