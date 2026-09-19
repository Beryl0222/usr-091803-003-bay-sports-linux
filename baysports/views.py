"""只读投影：从只追加事件折叠出当前状态或任意时间点状态。

赛后"从任意一场比赛进入"即调用 game_archive(store, game_id, as_of=...)：
as_of 取开球、加时结束或任意时刻，都能重建当时有效的名单、记录版本与处置。
"""

from .tournament import person_eligibility_at


# ----------------------------------------------------------------------
# 人员
# ----------------------------------------------------------------------

def build_person(store, person_id: str, as_of: str | None = None) -> dict:
    """折叠人员聚合：注册信息 + 最新通行材料/保险 + 有效资格列表。"""
    person: dict | None = None
    travel_doc = None
    insurance = None
    eligibility: list[dict] = []

    for e in store.replay("tournament", as_of=as_of):
        if e.etype == "PersonRegistered" and e.data["person_id"] == person_id:
            person = dict(e.data)

    if person is None:
        from .errors import NotFoundError
        raise NotFoundError("人员不存在", person_id=person_id)

    for e in store.replay(f"person:{person_id}", as_of=as_of):
        d = e.data
        if e.etype == "TravelDocumentRecorded":
            travel_doc = {k: d[k] for k in ("doc_type", "number", "valid_from", "valid_until")}
        elif e.etype == "InsuranceRecorded":
            insurance = {k: d[k] for k in
                         ("policy_no", "insurer", "valid_from", "valid_until", "coverage")}
        elif e.etype == "EligibilityGranted":
            eligibility.append({
                "type": d["type"], "valid_from": d["valid_from"],
                "valid_until": d["valid_until"], "basis": d.get("basis"),
                "granted_by": e.actor, "granted_at": e.occurred_at,
            })

    person["travel_document"] = travel_doc
    person["insurance"] = insurance
    person["eligibility"] = eligibility
    return person


def person_with_status(store, person_id: str, at: str) -> dict:
    person = build_person(store, person_id, as_of=at)
    checks = person_eligibility_at(person, at)
    person["eligibility_checks_at"] = checks
    person["eligible_now"] = all(checks.values())
    return person


# ----------------------------------------------------------------------
# 当场名单（时间点）
# ----------------------------------------------------------------------

def roster_at(store, game_id: str, at: str) -> dict:
    """重建 at 时刻有效的当场名单，并给出每条变化的溯源。"""
    submissions = [
        e for e in store.replay(f"game:{game_id}", as_of=at)
        if e.etype == "RosterSubmitted"
    ]
    if not submissions:
        return {"game_id": game_id, "as_of": at, "submitted": False,
                "players": [], "staff": [], "history": []}

    baseline = submissions[-1]
    players = list(baseline.data["player_ids"])
    staff = list(baseline.data["staff_ids"])
    history = [{
        "kind": "submitted", "at": baseline.occurred_at, "actor": baseline.actor,
        "basis": baseline.basis,
    }]

    for e in store.replay(f"game:{game_id}", as_of=at):
        if e.etype != "RosterChangeApproved":
            continue
        if e.occurred_at < baseline.occurred_at:
            continue  # 基线之后重新提交的名单会重置之前的换人
        d = e.data
        # 先判定离场者属于哪份名单，再移除，避免移除后无法定位
        person_out = d["person_out"]
        if person_out:
            if person_out in staff:
                staff.remove(person_out)
            elif person_out in players:
                players.remove(person_out)
        person_in = d["person_in"]
        if person_in not in players and person_in not in staff:
            incoming = build_person(store, person_in, as_of=at)
            (staff if incoming.get("role") == "staff" else players).append(person_in)
        history.append({
            "kind": "approved", "request_id": d["request_id"],
            "at": e.occurred_at, "actor": e.actor,
            "person_in": d["person_in"], "person_out": d["person_out"],
            "reason": d["reason"], "evidence": d.get("evidence"),
            "approved_by": d["approved_by"], "approved_by_role": d["approved_by_role"],
            "basis": d.get("basis"),
        })

    game = _game_scheduled(store, game_id)
    return {
        "game_id": game_id, "as_of": at, "submitted": True,
        "tipoff": game["tipoff"],
        "players": [_member_status(store, pid, game["tipoff"], at) for pid in players],
        "staff": [_member_status(store, pid, game["tipoff"], at) for pid in staff],
        "history": history,
    }


def _member_status(store, pid, tipoff, at):
    p = build_person(store, pid, as_of=at)
    return {
        "person_id": pid, "name": p.get("name"), "region": p.get("region"),
        "role": p.get("role"), "jersey_no": p.get("jersey_no"),
        "eligible_at_tipoff": all(person_eligibility_at(p, tipoff).values()),
    }


def change_explanation(store, request_id: str) -> dict:
    """回答：某次名单变化由谁、在什么依据下批准（或为何被拒）。"""
    request = None
    decision = None
    for e in store.replay():
        if e.etype == "RosterChangeRequested" and e.data["request_id"] == request_id:
            request = e
        if request is not None and e.etype in ("RosterChangeApproved", "RosterChangeRejected") \
                and e.data.get("request_id") == request_id:
            decision = e
    if request is None:
        from .errors import NotFoundError
        raise NotFoundError("名单变更申请不存在", request_id=request_id)

    out = {
        "request_id": request_id,
        "game_id": request.data["game_id"],
        "person_in": request.data["person_in"],
        "person_out": request.data["person_out"],
        "reason": request.data["reason"],
        "evidence": request.data.get("evidence"),
        "requested_by": request.actor,
        "requested_at": request.occurred_at,
        "status": request.data["status"],
    }
    if decision is not None:
        out["decided_at"] = decision.occurred_at
        if decision.etype == "RosterChangeApproved":
            out.update({
                "status": "approved",
                "approved_by": decision.data["approved_by"],
                "approved_by_role": decision.data["approved_by_role"],
                "basis": decision.data.get("basis"),
            })
        else:
            out.update({
                "status": "rejected",
                "rejected_by": decision.data["rejected_by"],
                "reject_reason": decision.data["reason"],
            })
    return out


# ----------------------------------------------------------------------
# 现场核验
# ----------------------------------------------------------------------

def verifications_for_game(store, game_id: str, as_of: str | None = None) -> list[dict]:
    rows = []
    for e in store.replay(f"game:{game_id}", as_of=as_of):
        if e.etype != "PersonVerified":
            continue
        d = e.data
        rows.append({
            "registration_no": d["registration_no"], "person_id": d["person_id"],
            "device_id": d["device_id"], "result": d["result"],
            "backfill": d.get("backfill", False), "note": d.get("note", ""),
            "occurred_at": e.occurred_at, "recorded_at": e.recorded_at,
            "actor": e.actor,
        })
    return rows


# ----------------------------------------------------------------------
# 裁判记录（多版本，原始记录不抹去）
# ----------------------------------------------------------------------

def referee_records(store, game_id: str, as_of: str | None = None) -> dict:
    versions = []
    for e in store.replay(f"game:{game_id}:record", as_of=as_of):
        if e.etype not in ("RefereeRecordSigned", "RefereeRecordAmended"):
            continue
        d = e.data
        versions.append({
            "version": d["version"],
            "kind": "signed" if e.etype == "RefereeRecordSigned" else d["amendment_type"],
            "payload": d["payload"],
            "referee": d.get("referee") or e.actor,
            "approved_by": d.get("approved_by"),
            "reason": d.get("reason"),
            "basis": d.get("basis"),
            "status": d.get("status"),
            "registration_no": e.registration_no,
            "occurred_at": e.occurred_at,
            "recorded_at": e.recorded_at,
        })
    return {
        "game_id": game_id,
        "versions": versions,
        "effective_version": versions[-1]["version"] if versions else None,
        "immutable_original_preserved": len(versions) >= 1,
    }


# ----------------------------------------------------------------------
# 通知与送达
# ----------------------------------------------------------------------

def notice_trace(store, notice_id: str) -> dict:
    """每版通知内容与该版各自的送达情况。"""
    versions = {}
    order = []
    for e in store.replay(f"notice:{notice_id}"):
        if e.etype == "NoticeIssued":
            versions[e.data["version"]] = {
                "version": e.data["version"],
                "supersedes": e.data.get("supersedes"),
                "title": e.data["title"], "body": e.data["body"],
                "languages": e.data["languages"], "audience": e.data["audience"],
                "issued_by": e.actor, "issued_at": e.occurred_at,
                "deliveries": [],
            }
            order.append(e.data["version"])
        elif e.etype == "NoticeDelivered":
            v = versions.get(e.data["version"])
            if v is not None:
                v["deliveries"].append({
                    "person_id": e.data["person_id"], "language": e.data["language"],
                    "channel": e.data["channel"], "status": e.data["status"],
                    "detail": e.data.get("detail", ""),
                    "at": e.occurred_at, "actor": e.actor,
                })
    result_versions = []
    for v in order:
        row = versions[v]
        statuses = [d["status"] for d in row["deliveries"]]
        row["delivery_summary"] = {
            "total": len(statuses),
            "delivered": sum(1 for s in statuses if s in ("delivered", "read")),
            "read": statuses.count("read"),
            "failed": sum(1 for s in statuses if s in ("failed", "bounced")),
            "pending": statuses.count("pending"),
        }
        result_versions.append(row)
    return {"notice_id": notice_id, "versions": result_versions}


# ----------------------------------------------------------------------
# 医疗
# ----------------------------------------------------------------------

def medical_case_view(store, case_id: str, as_of: str | None = None) -> dict:
    opened = None
    treatments = []
    closed = None
    costs = []
    for e in store.replay(f"medical:{case_id}", as_of=as_of):
        if e.etype == "MedicalCaseOpened":
            opened = e
        elif e.etype == "TreatmentGiven":
            treatments.append({
                "action": e.data["action"], "officer": e.data["officer"],
                "resources": e.data.get("resources", []),
                "at": e.occurred_at,
            })
        elif e.etype == "MedicalCaseClosed":
            closed = e
        elif e.etype == "CostAllocated":
            costs.append({
                "amount": e.data["amount"], "currency": e.data["currency"],
                "shares": e.data["shares"], "basis": e.data["basis"],
                "by": e.actor, "at": e.occurred_at,
            })
    if opened is None:
        from .errors import NotFoundError
        raise NotFoundError("医疗案例不存在", case_id=case_id)
    d = opened.data
    return {
        "case_id": case_id, "game_id": d["game_id"], "person_id": d["person_id"],
        "severity": d["severity"], "complaint": d["complaint"],
        "opened_by": d["officer"], "opened_at": opened.occurred_at,
        "treatments": treatments,
        "closed": closed is not None,
        "outcome": closed.data["outcome"] if closed else None,
        "closed_at": closed.occurred_at if closed else None,
        "cost_allocations": costs,
    }


def medical_cases_for_game(store, game_id: str, as_of: str | None = None) -> list[dict]:
    case_ids = set()
    for e in store.replay(as_of=as_of):
        if e.etype == "MedicalCaseOpened" and e.data["game_id"] == game_id:
            case_ids.add(e.data["case_id"])
    return [medical_case_view(store, cid, as_of=as_of) for cid in sorted(case_ids)]


# ----------------------------------------------------------------------
# 赛后整场归档
# ----------------------------------------------------------------------

def game_archive(store, game_id: str, as_of: str | None = None) -> dict:
    """从任意一场比赛进入，看到当时有效的全部事实。"""
    game = _game_scheduled(store, game_id)
    effective_at = as_of or game["tipoff"]
    return {
        "game": game,
        "as_of": effective_at,
        "roster": roster_at(store, game_id, effective_at),
        "verifications": verifications_for_game(store, game_id, as_of=effective_at),
        "referee_records": referee_records(store, game_id, as_of=effective_at),
        "medical_cases": medical_cases_for_game(store, game_id, as_of=effective_at),
    }


def _game_scheduled(store, game_id: str) -> dict:
    from .errors import NotFoundError
    for e in store.replay(f"game:{game_id}"):
        if e.etype == "GameScheduled":
            return e.data
    raise NotFoundError("比赛不存在", game_id=game_id)
