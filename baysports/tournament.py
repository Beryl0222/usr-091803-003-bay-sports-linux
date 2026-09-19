"""赛事协同领域服务。

所有写操作都翻译成只追加事件；规则判定集中在本模块。
关键不变量：
- 资格（通行证/签注、保险、参赛许可）均带有效期，窗口外不可进入当场名单；
- 替补进入名单必须经过"申请→批准"，批准时刻必须落在规则窗口内，且留下批准人与依据；
- 核验/裁判记录使用登记编号幂等，离线补传仍是同一条登记；
- 裁判原始记录签字后不可改，重赛/申诉/技术统计修正只产生新版本。
"""

from datetime import timedelta
from typing import Optional

from . import rules
from .clock import Clock, parse_iso, to_iso
from .errors import NotFoundError, RuleViolationError, ValidationError
from .store import EventStore


def _norm(value: str) -> str:
    """所有入参时间归一化为 UTC Z 字符串，保证时间线可按字典序比较。"""
    return to_iso(parse_iso(value))


class Tournament:
    def __init__(self, store: EventStore, clock: Clock):
        self.store = store
        self.clock = clock

    def _at(self, occurred_at: Optional[str]) -> str:
        return _norm(occurred_at) if occurred_at else self.clock.now_iso()

    # ==================================================================
    # 队伍与人员
    # ==================================================================

    def register_team(self, team_id, name, region, actor):
        if region not in rules.REGIONS:
            raise ValidationError("地区代码无效", region=region)
        return self.store.append(
            "tournament", "TeamRegistered",
            {"team_id": team_id, "name": name, "region": region},
            actor=actor,
        )

    def register_person(self, person_id, team_id, name, region, role, *,
                        jersey_no=None, contacts=None, languages=None,
                        emergency_contact=None, actor):
        if role not in rules.PERSON_ROLES:
            raise ValidationError("人员角色无效", role=role)
        if region not in rules.REGIONS:
            raise ValidationError("地区代码无效", region=region)
        languages = languages or [rules.DEFAULT_LANGUAGE]
        for lang in languages:
            if lang not in rules.LANGUAGES:
                raise ValidationError("语言代码无效", language=lang)
        return self.store.append(
            "tournament", "PersonRegistered",
            {
                "person_id": person_id, "team_id": team_id, "name": name,
                "region": region, "role": role, "jersey_no": jersey_no,
                "contacts": contacts or {}, "languages": languages,
                "emergency_contact": emergency_contact,
            },
            actor=actor,
        )

    def record_travel_document(self, person_id, doc_type, number,
                               valid_from, valid_until, actor):
        """港澳通行材料（通行证/签注等），带有效期。"""
        self._require_person(person_id)
        valid_from, valid_until = _norm(valid_from), _norm(valid_until)
        if parse_iso(valid_until) <= parse_iso(valid_from):
            raise ValidationError("通行材料有效期截止必须晚于生效")
        return self.store.append(
            f"person:{person_id}", "TravelDocumentRecorded",
            {"person_id": person_id, "doc_type": doc_type, "number": number,
             "valid_from": valid_from, "valid_until": valid_until},
            actor=actor,
        )

    def record_insurance(self, person_id, policy_no, insurer,
                         valid_from, valid_until, coverage, actor):
        """参赛保险，带有效期。"""
        self._require_person(person_id)
        valid_from, valid_until = _norm(valid_from), _norm(valid_until)
        if parse_iso(valid_until) <= parse_iso(valid_from):
            raise ValidationError("保险有效期截止必须晚于生效")
        return self.store.append(
            f"person:{person_id}", "InsuranceRecorded",
            {"person_id": person_id, "policy_no": policy_no, "insurer": insurer,
             "valid_from": valid_from, "valid_until": valid_until,
             "coverage": coverage},
            actor=actor,
        )

    def grant_eligibility(self, person_id, etype, valid_from, valid_until,
                          basis, actor):
        """授予带有效期的参赛资格（如注册确认、医疗准入、跨区参赛许可）。"""
        self._require_person(person_id)
        valid_from, valid_until = _norm(valid_from), _norm(valid_until)
        if parse_iso(valid_until) <= parse_iso(valid_from):
            raise ValidationError("资格有效期截止必须晚于生效")
        return self.store.append(
            f"person:{person_id}", "EligibilityGranted",
            {"person_id": person_id, "type": etype,
             "valid_from": valid_from, "valid_until": valid_until,
             "basis": basis},
            actor=actor, basis={"rulebook": rules.RULEBOOK_VERSION},
        )

    # ==================================================================
    # 票选名单
    # ==================================================================

    def nominate(self, person_id, votes, actor):
        """票选提名。"""
        self._require_person(person_id)
        if votes < 0:
            raise ValidationError("票数不能为负")
        return self.store.append(
            "tournament", "BallotNominated",
            {"person_id": person_id, "votes": votes,
             "stage": "nominated"},
            actor=actor,
        )

    def confirm_ballot(self, person_ids, actor, basis="票选结果确认"):
        """秘书处确认票选名单——此后替补只能从该名单池中选取。"""
        for pid in person_ids:
            self._require_person(pid)
        if len(set(person_ids)) != len(person_ids):
            raise ValidationError("票选名单存在重复人员")
        return self.store.append(
            "tournament", "BallotConfirmed",
            {"person_ids": person_ids, "stage": "confirmed",
             "rulebook": rules.RULEBOOK_VERSION},
            actor=actor, basis={"decision": basis},
        )

    # ==================================================================
    # 场馆时段 / 训练 / 医疗资源
    # ==================================================================

    def register_venue_slot(self, slot_id, venue, region, start, end,
                            purpose, capacity=0, actor=None):
        start, end = _norm(start), _norm(end)
        if end <= start:
            raise ValidationError("场馆时段结束必须晚于开始")
        for existing in self.store.replay("tournament"):
            if existing.etype != "VenueSlotRegistered":
                continue
            d = existing.data
            if d["venue"] == venue and _overlaps(start, end, d["start"], d["end"]):
                raise RuleViolationError(
                    "场馆时段冲突", venue=venue,
                    conflicting_slot=d["slot_id"],
                )
        return self.store.append(
            "tournament", "VenueSlotRegistered",
            {"slot_id": slot_id, "venue": venue, "region": region,
             "start": start, "end": end, "purpose": purpose,
             "capacity": capacity},
            actor=actor or "secretariat",
        )

    def register_medical_resource(self, resource_id, name, kind, region,
                                  available_from, available_until, capacity,
                                  actor):
        available_from, available_until = _norm(available_from), _norm(available_until)
        if available_until <= available_from:
            raise ValidationError("医疗资源可用时段无效")
        return self.store.append(
            "tournament", "MedicalResourceRegistered",
            {"resource_id": resource_id, "name": name, "kind": kind,
             "region": region, "available_from": available_from,
             "available_until": available_until, "capacity": capacity},
            actor=actor,
        )

    def book_medical_resource(self, booking_id, resource_id, game_id,
                              start, end, actor):
        """占用医疗资源（救护车/急救位），同时段不超容量。"""
        resources = {
            e.data["resource_id"]: e.data
            for e in self.store.replay("tournament")
            if e.etype == "MedicalResourceRegistered"
        }
        if resource_id not in resources:
            raise NotFoundError("医疗资源不存在", resource_id=resource_id)
        res = resources[resource_id]
        start, end = _norm(start), _norm(end)
        if start < res["available_from"] or end > res["available_until"]:
            raise RuleViolationError("预约超出医疗资源可用时段")
        used = 0
        for e in self.store.replay("tournament"):
            if e.etype != "MedicalResourceBooked" or e.data["resource_id"] != resource_id:
                continue
            if _overlaps(start, end, e.data["start"], e.data["end"]):
                used += 1
        if used >= res["capacity"]:
            raise RuleViolationError(
                "医疗资源容量已满", resource_id=resource_id, capacity=res["capacity"],
            )
        return self.store.append(
            "tournament", "MedicalResourceBooked",
            {"booking_id": booking_id, "resource_id": resource_id,
             "game_id": game_id, "start": start, "end": end},
            actor=actor,
        )

    # ==================================================================
    # 比赛与当场名单
    # ==================================================================

    def schedule_game(self, game_id, label, slot_id, tipoff,
                      team_ids, actor, emergency_window_minutes=20):
        slots = {
            e.data["slot_id"]: e.data
            for e in self.store.replay("tournament")
            if e.etype == "VenueSlotRegistered"
        }
        if slot_id not in slots:
            raise NotFoundError("场馆时段不存在", slot_id=slot_id)
        slot = slots[slot_id]
        tipoff = _norm(tipoff)
        if tipoff < slot["start"] or tipoff >= slot["end"]:
            raise RuleViolationError("开球时间不在已登记的场馆时段内")
        return self.store.append(
            f"game:{game_id}", "GameScheduled",
            {"game_id": game_id, "label": label, "slot_id": slot_id,
             "venue": slot["venue"], "region": slot["region"],
             "tipoff": tipoff, "team_ids": team_ids,
             "emergency_deadline": to_iso(parse_iso(tipoff) + timedelta(minutes=emergency_window_minutes))},
            actor=actor,
        )

    def submit_roster(self, game_id, player_ids, staff_ids, actor, at=None):
        """提交当场名单（开球前常规窗口内）。所有成员资格须在开球时有效。"""
        at = self._at(at)
        game = self._require_game(game_id)
        window = rules.ROSTER_CHANGE_WINDOW["normal"]
        deadline = parse_iso(game["tipoff"]) - timedelta(minutes=window["lead_minutes"])
        if parse_iso(at) > deadline:
            raise RuleViolationError(
                "已过常规名单提交窗口（开球前90分钟）",
                deadline=to_iso(deadline), at=at,
            )
        self._assert_members_eligible(game_id, player_ids + staff_ids, game["tipoff"])
        return self.store.append(
            f"game:{game_id}", "RosterSubmitted",
            {"game_id": game_id, "player_ids": player_ids, "staff_ids": staff_ids,
             "rulebook": rules.RULEBOOK_VERSION},
            actor=actor, occurred_at=at,
            basis={"window": "normal", "rulebook": rules.RULEBOOK_VERSION},
        )

    def request_roster_change(self, request_id, game_id, person_in,
                              person_out, reason, evidence, actor, at=None,
                              note=""):
        """申请名单变更。进入当场名单的批准在规则窗口内完成。"""
        at = self._at(at)
        game = self._require_game(game_id)
        if reason not in rules.CHANGE_REASONS:
            raise ValidationError("名单变更原因无效", reason=reason)
        self._require_person(person_in)
        if person_out:
            self._require_person(person_out)
        if reason in ("medical", "emergency") and not evidence:
            raise RuleViolationError(f"{reason} 类换人必须附证明材料编号")
        # 替补必须来自已确认的票选名单池
        pool = self._confirmed_pool()
        if pool is not None and person_in not in pool:
            raise RuleViolationError(
                "替补不在已确认的票选名单内", person_id=person_in,
            )
        return self.store.append(
            f"game:{game_id}", "RosterChangeRequested",
            {"request_id": request_id, "game_id": game_id,
             "person_in": person_in, "person_out": person_out,
             "reason": reason, "evidence": evidence, "note": note,
             "status": "requested"},
            actor=actor, occurred_at=at,
        )

    def approve_roster_change(self, request_id, approver, approver_role,
                              basis, at=None):
        """批准名单变更——此刻再次校验规则窗口与资格，二者都满足才进入名单。"""
        at = self._at(at)
        req = self._find_change_request(request_id)
        if req is None:
            raise NotFoundError("名单变更申请不存在", request_id=request_id)
        if self._find_change_decision(request_id) is not None:
            raise RuleViolationError("该申请已处理")
        game_id = req.data["game_id"]
        game = self._require_game(game_id)

        allowed_approvers = {
            "normal": {"secretariat_chief", "referee"},
            "medical": {"medical_officer", "secretariat_chief"},
            "emergency": {"referee", "secretariat_chief"},
        }[req.data["reason"]]
        if approver_role not in allowed_approvers:
            raise RuleViolationError(
                "该角色无权批准此类名单变更",
                reason=req.data["reason"], role=approver_role,
            )

        tipoff = parse_iso(game["tipoff"])
        now = parse_iso(at)
        reason = req.data["reason"]
        window = rules.ROSTER_CHANGE_WINDOW[reason]
        if not window["after_tipoff"]:
            deadline = tipoff - timedelta(minutes=window["lead_minutes"])
            if now > deadline:
                raise RuleViolationError(
                    f"已过{reason}换人窗口", deadline=to_iso(deadline), at=at,
                )
        else:
            if now < tipoff:
                raise RuleViolationError("紧急窗口开球后方可使用")
            emergency_deadline = parse_iso(game["emergency_deadline"])
            if now > emergency_deadline:
                raise RuleViolationError(
                    "紧急换人窗口已关闭（第一节结束）",
                    deadline=game["emergency_deadline"], at=at,
                )

        self._assert_members_eligible(
            game_id, [req.data["person_in"]], game["tipoff"],
        )

        approval = {
            "request_id": request_id, "game_id": game_id,
            "person_in": req.data["person_in"],
            "person_out": req.data["person_out"],
            "reason": req.data["reason"], "evidence": req.data["evidence"],
            "status": "approved",
            "approved_by": approver, "approved_by_role": approver_role,
            "basis": {"rulebook": rules.RULEBOOK_VERSION, "decision": basis},
        }
        return self.store.append(
            f"game:{game_id}", "RosterChangeApproved", approval,
            actor=approver, occurred_at=at,
            basis=approval["basis"],
        )

    def reject_roster_change(self, request_id, approver, reason, at=None):
        at = self._at(at)
        req = self._find_change_request(request_id)
        if req is None:
            raise NotFoundError("名单变更申请不存在", request_id=request_id)
        if self._find_change_decision(request_id) is not None:
            raise RuleViolationError("该申请已处理")
        return self.store.append(
            f"game:{req.data['game_id']}", "RosterChangeRejected",
            {"request_id": request_id, "game_id": req.data["game_id"],
             "status": "rejected", "rejected_by": approver, "reason": reason},
            actor=approver, occurred_at=at,
        )

    # ==================================================================
    # 现场核验（支持设备失联补传，一次登记）
    # ==================================================================

    def verify_person(self, registration_no, game_id, person_id, device_id,
                      result, occurred_at, actor, note=""):
        """登记一次现场核验。

        registration_no 由核验设备生成（如 DEV-时间戳-流水）。设备失联后补传：
        - 同一 registration_no、同一内容：回放原登记，不产生第二条；
        - 同一 registration_no、不同内容：拒绝，首次登记不可被覆盖；
        - 补传发生时间距现场时间超过时限：拒绝。
        """
        self._require_game(game_id)
        self._require_person(person_id)
        if result not in ("pass", "fail", "manual_pass"):
            raise ValidationError("核验结果无效", result=result)
        occurred_at = self._at(occurred_at)
        recorded_at = self.clock.now_iso()
        delay = parse_iso(recorded_at) - parse_iso(occurred_at)
        if timedelta(0) <= delay > timedelta(minutes=rules.VERIFICATION_BACKFILL_LIMIT_MINUTES):
            raise RuleViolationError(
                "补传超过允许时限",
                limit_minutes=rules.VERIFICATION_BACKFILL_LIMIT_MINUTES,
            )
        if delay < timedelta(0):
            raise ValidationError("核验发生时间不能晚于系统时间")
        backfill = delay > timedelta(minutes=2)
        return self.store.append(
            f"game:{game_id}", "PersonVerified",
            {"registration_no": registration_no, "game_id": game_id,
             "person_id": person_id, "device_id": device_id, "result": result,
             "backfill": backfill, "note": note},
            actor=actor, occurred_at=occurred_at,
            registration_no=registration_no,
        )

    # ==================================================================
    # 多语言通知（版本化，保留每版送达）
    # ==================================================================

    def issue_notice(self, notice_id, title, body, languages, audience,
                     actor, at=None, supersedes=None):
        """发布通知。修订以新版本生效，旧版本与送达记录保留。"""
        at = self._at(at)
        for lang in languages:
            if lang not in rules.LANGUAGES:
                raise ValidationError("通知语言无效", language=lang)
        versions = [
            e for e in self.store.replay(f"notice:{notice_id}")
            if e.etype == "NoticeIssued"
        ]
        version = len(versions) + 1
        if supersedes is None and version > 1:
            supersedes = version - 1
        if supersedes is not None and supersedes != version - 1:
            raise ValidationError("通知只能紧接最新版本修订")
        return self.store.append(
            f"notice:{notice_id}", "NoticeIssued",
            {"notice_id": notice_id, "version": version,
             "supersedes": supersedes, "title": title, "body": body,
             "languages": languages, "audience": audience},
            actor=actor, occurred_at=at,
        )

    def record_delivery(self, notice_id, version, person_id, language,
                        channel, status, occurred_at, actor,
                        registration_no=None, detail=""):
        """登记一条送达回执（每版通知各自累计）。"""
        if language not in rules.LANGUAGES:
            raise ValidationError("送达语言无效", language=language)
        if channel not in rules.DELIVERY_CHANNELS:
            raise ValidationError("送达渠道无效", channel=channel)
        if status not in ("delivered", "failed", "bounced", "pending", "read"):
            raise ValidationError("送达状态无效", status=status)
        versions = {
            e.data["version"] for e in self.store.replay(f"notice:{notice_id}")
            if e.etype == "NoticeIssued"
        }
        if version not in versions:
            raise NotFoundError("通知版本不存在", notice_id=notice_id, version=version)
        return self.store.append(
            f"notice:{notice_id}", "NoticeDelivered",
            {"notice_id": notice_id, "version": version, "person_id": person_id,
             "language": language, "channel": channel, "status": status,
             "detail": detail},
            actor=actor, occurred_at=self._at(occurred_at),
            registration_no=registration_no,
        )

    # ==================================================================
    # 医疗处置与费用分摊
    # ==================================================================

    def open_medical_case(self, case_id, game_id, person_id, severity,
                          complaint, officer, occurred_at):
        self._require_game(game_id)
        self._require_person(person_id)
        if severity not in ("mild", "moderate", "serious", "emergency"):
            raise ValidationError("伤情分级无效", severity=severity)
        return self.store.append(
            f"medical:{case_id}", "MedicalCaseOpened",
            {"case_id": case_id, "game_id": game_id, "person_id": person_id,
             "severity": severity, "complaint": complaint,
             "officer": officer},
            actor=officer, occurred_at=self._at(occurred_at),
        )

    def add_treatment(self, case_id, action, officer, occurred_at, resources=None):
        self._require_case(case_id)
        return self.store.append(
            f"medical:{case_id}", "TreatmentGiven",
            {"case_id": case_id, "action": action, "officer": officer,
             "resources": resources or []},
            actor=officer, occurred_at=self._at(occurred_at),
        )

    def close_medical_case(self, case_id, outcome, officer, occurred_at):
        self._require_case(case_id)
        return self.store.append(
            f"medical:{case_id}", "MedicalCaseClosed",
            {"case_id": case_id, "outcome": outcome, "officer": officer},
            actor=officer, occurred_at=self._at(occurred_at),
        )

    def allocate_cost(self, case_id, amount, currency, shares, basis, actor,
                      occurred_at=None):
        """登记费用分摊。shapes 形如 {'GD': 60, 'HK': 40, 'MO': 0}（金额或比例）。

        比例制时三项之和必须为 100；金额制之和必须等于总额。
        """
        self._require_case(case_id)
        for region in shares:
            if region not in rules.REGIONS:
                raise ValidationError("分摊地区无效", region=region)
        total = sum(float(v) for v in shares.values())
        if basis.get("mode") == "percent":
            if abs(total - 100) > 0.001:
                raise ValidationError("比例分摊之和必须为100", total=total)
        else:
            if abs(total - float(amount)) > 0.01:
                raise ValidationError("金额分摊之和必须等于费用总额",
                                      total=total, amount=amount)
        return self.store.append(
            f"medical:{case_id}", "CostAllocated",
            {"case_id": case_id, "amount": amount, "currency": currency,
             "shares": shares, "basis": basis},
            actor=actor, occurred_at=self._at(occurred_at),
        )

    # ==================================================================
    # 裁判记录：签字不可变，修正以新版本生效
    # ==================================================================

    def sign_referee_record(self, game_id, payload, referee, occurred_at,
                            registration_no=None):
        """裁判对当场记录（比分、犯规、技术统计）签字。签字后即冻结。"""
        self._require_game(game_id)
        versions = self.store.replay(f"game:{game_id}:record")
        signed = [e for e in versions if e.etype == "RefereeRecordSigned"]
        if signed:
            raise RuleViolationError("裁判记录已签字，修正须提交新版本")
        return self.store.append(
            f"game:{game_id}:record", "RefereeRecordSigned",
            {"game_id": game_id, "version": 1, "payload": payload,
             "referee": referee, "status": "signed"},
            actor=referee, occurred_at=self._at(occurred_at),
            registration_no=registration_no,
        )

    def amend_referee_record(self, game_id, new_payload, amendment_type,
                             reason, approver, basis, occurred_at,
                             registration_no=None):
        """重赛/比分申诉/技术统计修正：新增版本，旧版保留。"""
        if amendment_type not in ("replay", "score_protest", "stat_correction"):
            raise ValidationError("修正类型无效", amendment_type=amendment_type)
        versions = self.store.replay(f"game:{game_id}:record")
        signed = [e for e in versions if e.etype in
                  ("RefereeRecordSigned", "RefereeRecordAmended")]
        if not signed:
            raise RuleViolationError("尚无签字记录，不能修正")
        last_version = max(e.data["version"] for e in signed)
        return self.store.append(
            f"game:{game_id}:record", "RefereeRecordAmended",
            {"game_id": game_id, "version": last_version + 1,
             "payload": new_payload, "amendment_type": amendment_type,
             "reason": reason, "approved_by": approver,
             "basis": {"rulebook": rules.RULEBOOK_VERSION, **basis},
             "status": "amended"},
            actor=approver, occurred_at=self._at(occurred_at),
            registration_no=registration_no,
        )

    # ==================================================================
    # 内部查询
    # ==================================================================

    def _require_person(self, person_id):
        for e in self.store.replay("tournament"):
            if e.etype == "PersonRegistered" and e.data["person_id"] == person_id:
                return e.data
        raise NotFoundError("人员不存在", person_id=person_id)

    def _require_game(self, game_id):
        for e in self.store.replay(f"game:{game_id}"):
            if e.etype == "GameScheduled":
                return e.data
        raise NotFoundError("比赛不存在", game_id=game_id)

    def _require_case(self, case_id):
        events = self.store.replay(f"medical:{case_id}")
        if not events:
            raise NotFoundError("医疗案例不存在", case_id=case_id)

    def _find_change_request(self, request_id):
        for e in self.store.replay():
            if e.etype == "RosterChangeRequested" and e.data["request_id"] == request_id:
                return e
        return None

    def _find_change_decision(self, request_id):
        """申请事件不可变，其当前状态从后续批准/拒绝事件派生。"""
        for e in self.store.replay():
            if e.etype in ("RosterChangeApproved", "RosterChangeRejected") \
                    and e.data.get("request_id") == request_id:
                return e
        return None

    def _confirmed_pool(self):
        pool = None
        for e in self.store.replay("tournament"):
            if e.etype == "BallotConfirmed":
                pool = e.data["person_ids"]
        return pool

    def _assert_members_eligible(self, game_id, person_ids, tipoff):
        """开球时每名成员：通行材料有效、保险覆盖至赛后、参赛许可有效。"""
        from .views import build_person

        for pid in person_ids:
            person = build_person(self.store, pid)
            checks = person_eligibility_at(person, tipoff)
            failed = [name for name, ok in checks.items() if not ok]
            if failed:
                raise RuleViolationError(
                    "成员参赛资格在开球时不满足",
                    person_id=pid, failed=failed, tipoff=tipoff,
                )


def person_eligibility_at(person: dict, at: str) -> dict:
    """返回各资格项在 at 时刻是否有效。"""
    moment = parse_iso(at)
    doc = person.get("travel_document")
    ins = person.get("insurance")
    grants = person.get("eligibility", [])

    doc_ok = bool(doc) and parse_iso(doc["valid_from"]) <= moment <= parse_iso(doc["valid_until"])
    insurance_until = parse_iso(ins["valid_until"]) if ins else None
    ins_ok = bool(ins) and parse_iso(ins["valid_from"]) <= moment and (
        insurance_until >= moment + timedelta(days=rules.INSURANCE_MUST_COVER_DAYS_AFTER_GAME)
    )
    grant_ok = any(
        parse_iso(g["valid_from"]) <= moment <= parse_iso(g["valid_until"])
        for g in grants
    )
    return {"travel_document": doc_ok, "insurance": ins_ok, "eligibility_grant": grant_ok}


def _overlaps(start_a, end_a, start_b, end_b) -> bool:
    return start_a < end_b and start_b < end_a
