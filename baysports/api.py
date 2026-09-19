"""HTTP 适配层。

- 写接口全部把 JSON 请求翻译成领域命令，业务拒绝映射为稳定错误码；
- 读接口按 X-Staff-* 头构造 Viewer，个人资料查询强制最小字段视图；
- /health 保持与基础契约一致；未知路径一律 404。
"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import views
from .auth import Viewer, authorize, person_view
from .clock import Clock
from .errors import DomainError
from .store import EventStore
from .tournament import Tournament

SERVICE_ID = "bay-sports"
SERVICE_NAME = "湾区赛事协同台账"


class App:
    """应用装配根：事件库 + 时钟 + 领域服务。"""

    def __init__(self, clock: Clock | None = None, store_path: str | None = None):
        self.clock = clock or Clock()
        self.store = EventStore(self.clock, path=store_path)
        self.tournament = Tournament(self.store, self.clock)


# ----------------------------------------------------------------------
# 路由表：(method, compiled) -> handler 名
# ----------------------------------------------------------------------

_ROUTES = []


def route(method, pattern):
    regex = re.compile("^" + re.sub(r"{([^/}]+)}", r"(?P<\1>[^/]+)", pattern) + "$")

    def register(func):
        _ROUTES.append((method, regex, func))
        return func

    return register


class Handler(BaseHTTPRequestHandler):
    app: App  # 由 make_server 注入到类上

    # ---- 基础 ----------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        if parsed.path == "/health" and method == "GET":
            self._write_json(200, {"status": "ok", "service": SERVICE_ID,
                                   "name": SERVICE_NAME})
            return
        for verb, regex, func in _ROUTES:
            if verb != method:
                continue
            match = regex.match(parsed.path)
            if match:
                try:
                    body = self._read_body() if method == "POST" else {}
                    if method == "POST" and body is None:
                        return  # 坏请求已响应
                    query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                    func(self, body=body, query=query, **match.groupdict())
                except DomainError as error:
                    self._write_json(error.http_status, error.to_dict())
                except (ValueError, KeyError, TypeError) as error:
                    self._write_json(400, {"error": "bad_request", "message": str(error)})
                return
        self._write_json(404, {"error": "not_found", "message": "未知路径"})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as error:
            self._write_json(400, {"error": "bad_json", "message": str(error)})
            return None
        if not isinstance(data, dict):
            self._write_json(400, {"error": "bad_request", "message": "请求体必须是对象"})
            return None
        return data

    def _write_json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return

    # ---- 调用者 --------------------------------------------------------

    def _actor(self, body):
        staff_id = self.headers.get("X-Staff-Id") or body.get("actor")
        if not staff_id:
            from .errors import AuthorizationError
            raise AuthorizationError("缺少操作者身份（X-Staff-Id）")
        return staff_id

    def _viewer(self):
        staff_id = self.headers.get("X-Staff-Id")
        role = self.headers.get("X-Role", "")
        if not staff_id:
            from .errors import AuthorizationError
            raise AuthorizationError("缺少管理员身份（X-Staff-Id / X-Role）")
        return Viewer(
            staff_id=staff_id,
            name=self.headers.get("X-Staff-Name", staff_id),
            region=self.headers.get("X-Region", "GD"),
            role=role,
        )


# ======================================================================
# 写接口
# ======================================================================

@route("POST", "/teams")
def _(h: Handler, body, query):
    t = h.app.tournament
    e = t.register_team(body["team_id"], body["name"], body["region"], h._actor(body))
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/persons")
def _(h: Handler, body, query):
    t = h.app.tournament
    e = t.register_person(
        body["person_id"], body["team_id"], body["name"], body["region"],
        body["role"], jersey_no=body.get("jersey_no"),
        contacts=body.get("contacts"), languages=body.get("languages"),
        emergency_contact=body.get("emergency_contact"), actor=h._actor(body),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/persons/{person_id}/travel-document")
def _(h, body, query, person_id):
    e = h.app.tournament.record_travel_document(
        person_id, body["doc_type"], body["number"],
        body["valid_from"], body["valid_until"], h._actor(body),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/persons/{person_id}/insurance")
def _(h, body, query, person_id):
    e = h.app.tournament.record_insurance(
        person_id, body["policy_no"], body["insurer"],
        body["valid_from"], body["valid_until"], body.get("coverage", {}),
        h._actor(body),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/persons/{person_id}/eligibility")
def _(h, body, query, person_id):
    e = h.app.tournament.grant_eligibility(
        person_id, body["type"], body["valid_from"], body["valid_until"],
        body.get("basis", {}), h._actor(body),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/ballot/nominations")
def _(h, body, query):
    e = h.app.tournament.nominate(body["person_id"], int(body["votes"]), h._actor(body))
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/ballot/confirm")
def _(h, body, query):
    e = h.app.tournament.confirm_ballot(
        body["person_ids"], h._actor(body), body.get("basis", "票选结果确认"),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/venue-slots")
def _(h, body, query):
    e = h.app.tournament.register_venue_slot(
        body["slot_id"], body["venue"], body["region"],
        body["start"], body["end"], body["purpose"],
        capacity=int(body.get("capacity", 0)), actor=h._actor(body),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/medical-resources")
def _(h, body, query):
    e = h.app.tournament.register_medical_resource(
        body["resource_id"], body["name"], body["kind"], body["region"],
        body["available_from"], body["available_until"], int(body["capacity"]),
        h._actor(body),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/medical-resources/bookings")
def _(h, body, query):
    e = h.app.tournament.book_medical_resource(
        body["booking_id"], body["resource_id"], body["game_id"],
        body["start"], body["end"], h._actor(body),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/games")
def _(h, body, query):
    e = h.app.tournament.schedule_game(
        body["game_id"], body["label"], body["slot_id"], body["tipoff"],
        body["team_ids"], h._actor(body),
        int(body.get("emergency_window_minutes", 20)),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/games/{game_id}/roster")
def _(h, body, query, game_id):
    e = h.app.tournament.submit_roster(
        game_id, body["player_ids"], body.get("staff_ids", []),
        h._actor(body), at=body.get("at"),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/roster-changes")
def _(h, body, query):
    e = h.app.tournament.request_roster_change(
        body["request_id"], body["game_id"], body["person_in"],
        body.get("person_out"), body["reason"], body.get("evidence", ""),
        h._actor(body), at=body.get("at"), note=body.get("note", ""),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/roster-changes/{request_id}/approve")
def _(h, body, query, request_id):
    viewer = h._viewer()
    e = h.app.tournament.approve_roster_change(
        request_id, viewer.staff_id, viewer.role,
        body.get("basis", body.get("decision", "")), at=body.get("at"),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/roster-changes/{request_id}/reject")
def _(h, body, query, request_id):
    e = h.app.tournament.reject_roster_change(
        request_id, h._actor(body), body.get("reason", ""), at=body.get("at"),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/verifications")
def _(h, body, query):
    before = h.app.store.version()
    e = h.app.tournament.verify_person(
        body["registration_no"], body["game_id"], body["person_id"],
        body["device_id"], body["result"], body["occurred_at"],
        h._actor(body), note=body.get("note", ""),
    )
    # seq 未前进即幂等命中：回放的是首次登记，仍只算一次登记
    h._write_json(201, {"event": e.to_dict(), "replayed": e.seq <= before})


@route("POST", "/notices")
def _(h, body, query):
    e = h.app.tournament.issue_notice(
        body["notice_id"], body["title"], body["body"],
        body["languages"], body.get("audience", {}), h._actor(body),
        at=body.get("at"), supersedes=body.get("supersedes"),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/notices/{notice_id}/versions/{version}/deliveries")
def _(h, body, query, notice_id, version):
    e = h.app.tournament.record_delivery(
        notice_id, int(version), body["person_id"], body["language"],
        body["channel"], body["status"], body["occurred_at"], h._actor(body),
        registration_no=body.get("registration_no"),
        detail=body.get("detail", ""),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/medical-cases")
def _(h, body, query):
    e = h.app.tournament.open_medical_case(
        body["case_id"], body["game_id"], body["person_id"],
        body["severity"], body["complaint"], h._actor(body),
        body["occurred_at"],
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/medical-cases/{case_id}/treatments")
def _(h, body, query, case_id):
    e = h.app.tournament.add_treatment(
        case_id, body["action"], h._actor(body), body["occurred_at"],
        resources=body.get("resources"),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/medical-cases/{case_id}/close")
def _(h, body, query, case_id):
    e = h.app.tournament.close_medical_case(
        case_id, body["outcome"], h._actor(body), body["occurred_at"],
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/medical-cases/{case_id}/costs")
def _(h, body, query, case_id):
    e = h.app.tournament.allocate_cost(
        case_id, body["amount"], body.get("currency", "CNY"),
        body["shares"], body.get("basis", {}), h._actor(body),
        occurred_at=body.get("at"),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/games/{game_id}/referee-records")
def _(h, body, query, game_id):
    e = h.app.tournament.sign_referee_record(
        game_id, body["payload"], h._actor(body), body["occurred_at"],
        registration_no=body.get("registration_no"),
    )
    h._write_json(201, {"event": e.to_dict()})


@route("POST", "/games/{game_id}/referee-records/amend")
def _(h, body, query, game_id):
    e = h.app.tournament.amend_referee_record(
        game_id, body["payload"], body["amendment_type"], body["reason"],
        h._actor(body), body.get("basis", {}), body["occurred_at"],
        registration_no=body.get("registration_no"),
    )
    h._write_json(201, {"event": e.to_dict()})


# ======================================================================
# 读接口（均要求管理员身份；个人资料另有目的与地域限制）
# ======================================================================

@route("GET", "/persons/{person_id}")
def _(h, body, query, person_id):
    viewer = h._viewer()
    purpose = query.get("purpose", "")
    person = views.build_person(h.app.store, person_id, as_of=query.get("at"))
    authorize(viewer, purpose, person.get("region"))
    payload = person_view(person, purpose)
    if query.get("at"):
        from .tournament import person_eligibility_at
        payload["eligibility_checks_at"] = person_eligibility_at(person, query["at"])
    h._write_json(200, {"person": payload,
                        "viewer": {"staff_id": viewer.staff_id, "purpose": purpose}})


@route("GET", "/games/{game_id}/roster")
def _(h, body, query, game_id):
    h._viewer()
    at = query.get("at") or h.app.clock.now_iso()
    h._write_json(200, views.roster_at(h.app.store, game_id, at))


@route("GET", "/games/{game_id}/archive")
def _(h, body, query, game_id):
    h._viewer()
    h._write_json(200, views.game_archive(h.app.store, game_id, query.get("as_of")))


@route("GET", "/games/{game_id}/verifications")
def _(h, body, query, game_id):
    h._viewer()
    h._write_json(200, {"verifications": views.verifications_for_game(
        h.app.store, game_id, as_of=query.get("at"))})


@route("GET", "/games/{game_id}/referee-records")
def _(h, body, query, game_id):
    h._viewer()
    h._write_json(200, views.referee_records(h.app.store, game_id, query.get("at")))


@route("GET", "/roster-changes/{request_id}")
def _(h, body, query, request_id):
    h._viewer()
    h._write_json(200, views.change_explanation(h.app.store, request_id))


@route("GET", "/notices/{notice_id}")
def _(h, body, query, notice_id):
    h._viewer()
    h._write_json(200, views.notice_trace(h.app.store, notice_id))


@route("GET", "/medical-cases/{case_id}")
def _(h, body, query, case_id):
    h._viewer()
    h._write_json(200, views.medical_case_view(h.app.store, case_id))


@route("GET", "/events")
def _(h, body, query):
    viewer = h._viewer()
    if viewer.role not in ("auditor", "secretariat_chief"):
        from .errors import AuthorizationError
        raise AuthorizationError("仅审计岗可导出事件全量日志")
    h._write_json(200, {"events": [e.to_dict() for e in h.app.store.all()],
                        "version": h.app.store.version()})


def make_server(host: str, port: int, app: App | None = None):
    app = app or App()
    Handler.app = app
    return ThreadingHTTPServer((host, port), Handler)
