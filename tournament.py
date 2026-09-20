"""赛事协同领域服务。

设计要点（对应赛事秘书处的实际诉求）：

1. 一切状态变化都是只追加事件（见 ledger.py），重赛、比分申诉、技术统计修正以
   “新版本”生效，裁判的原始记录永远保留可查；
2. 参赛资格 = 人 + 通行材料 + 保险，三者都带有效期，所有查询都可指定“回到某时刻”；
3. 替补进入当场名单必须落在当场比赛公布的规则窗口内，且申请—批准—生效留痕可串联；
4. 票选名单是名单基线，替补在其上变更；
5. 现场设备用登记号做幂等键，失联补传只产生一次登记；
6. 跨地区管理员按属地 + 字段级最小必要授权查阅个人资料；
7. 每条名单变化都能回答“谁、依据什么、在何时批准”。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from ledger import IdempotentReplay, Ledger, now_ts, parse_ts

# --- 角色与字段级授权 ---------------------------------------------------------

ROLE_ASSOCIATION = "association_admin"   # 协会：全域全字段（赛后追溯）
ROLE_REGION = "region_admin"             # 地区管理员：仅本属地、脱敏字段
ROLE_TEAM = "team_official"              # 队伍工作人员：本队
ROLE_MEDICAL = "medical_officer"         # 医疗官：医疗字段
ROLE_VENUE = "venue_staff"               # 场馆/核验岗：仅核验所需
ROLE_REFEREE = "referee"                 # 裁判：当场名单

# 个人资料字段分级；查看者角色只能取到被授予的级别
FIELD_LEVELS = {
    "name": "identity",
    "jersey_number": "identity",
    "photo": "identity",
    "phone": "contact",
    "email": "contact",
    "emergency_contact": "medical",
    "blood_type": "medical",
    "allergies": "medical",
    "birth_date": "sensitive",
    "id_number": "document",
    "address": "sensitive",
}

ROLE_FIELD_GRANTS = {
    ROLE_ASSOCIATION: {"identity", "contact", "medical", "sensitive", "document", "internal"},
    ROLE_REGION: {"identity"},
    ROLE_TEAM: {"identity", "contact", "internal"},
    ROLE_MEDICAL: {"identity", "medical", "contact"},
    ROLE_VENUE: {"identity"},
    ROLE_REFEREE: {"identity"},
}

SUB_MEDICAL_REASONS = {"injury", "illness"}


class DomainError(Exception):
    """规则校验失败（拒绝写入，台账不产生事件）。"""


# --- 领域服务 -----------------------------------------------------------------


class TournamentService:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    # ===== 通用写入（HTTP 层统一入口） =====
    def command(self, cmd_type: str, actor: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        handler = getattr(self, f"cmd_{cmd_type}", None)
        if handler is None:
            raise DomainError(f"未知命令类型: {cmd_type}")
        options = {key: value for key, value in kw.items() if value is not None}
        event = handler(actor, payload, **options)
        return event.to_dict()

    # ----- 人员与参赛资格 -----
    def cmd_register_person(self, actor, p, *, occurred_at=None, idempotency_key=None):
        for key in ("person_id", "name", "role", "team_id", "region"):
            if not p.get(key):
                raise DomainError(f"缺少字段: {key}")
        if p["role"] not in ("player", "coach", "staff", "medical", "referee"):
            raise DomainError("人员角色不合法")
        events = [
            self.ledger.append(
                "PersonRegistered", actor,
                {k: p[k] for k in ("person_id", "name", "role", "team_id", "region")}
                | {"pii": p.get("pii", {}), "jersey_number": p.get("jersey_number")},
                occurred_at=occurred_at, idempotency_key=idempotency_key,
            )
        ]
        cred = p.get("credential")
        if cred:
            self._require_validity(cred)
            events.append(self.ledger.append(
                "CredentialRecorded", actor,
                {"person_id": p["person_id"], **cred}, occurred_at=occurred_at,
            ))
        insurance = p.get("insurance")
        if insurance:
            self._require_validity(insurance)
            events.append(self.ledger.append(
                "InsuranceBound", actor,
                {"person_id": p["person_id"], **insurance}, occurred_at=occurred_at,
            ))
        return events[-1]

    def cmd_record_credential(self, actor, p, *, occurred_at=None):
        """补登/更新通行材料（如港澳通行证签注），旧材料记录不删除。"""
        self._require_person(p["person_id"])
        self._require_validity(p)
        return self.ledger.append("CredentialRecorded", actor, p, occurred_at=occurred_at)

    def cmd_bind_insurance(self, actor, p, *, occurred_at=None):
        """保险可按个人或整队投保；按队投保时带 covered 名单。"""
        self._require_validity(p)
        if not p.get("person_id") and not p.get("team_id"):
            raise DomainError("保险必须挂靠个人或队伍")
        return self.ledger.append("InsuranceBound", actor, p, occurred_at=occurred_at)

    # ----- 管理员账户（跨地区查阅授权） -----
    def cmd_register_admin(self, actor, p, *, occurred_at=None):
        if p.get("role") not in ROLE_FIELD_GRANTS:
            raise DomainError("管理员角色不合法")
        p.setdefault("regions", [])
        p.setdefault("team_ids", [])
        return self.ledger.append("AdminRegistered", actor, p, occurred_at=occurred_at)

    # ----- 场馆与赛程 -----
    def cmd_book_venue_slot(self, actor, p, *, occurred_at=None):
        self._require_range(p)
        slot_id = p["slot_id"]
        for e in self.ledger.events(type_in={"VenueSlotBooked"}):
            if e.payload["venue_id"] == p["venue_id"] and self._ranges_overlap(
                e.payload["starts_at"], e.payload["ends_at"], p["starts_at"], p["ends_at"]
            ):
                raise DomainError(f"场馆时段与已登记时段 {slot_id} 冲突")
        return self.ledger.append("VenueSlotBooked", actor, p, occurred_at=occurred_at)

    def cmd_schedule_game(self, actor, p, *, occurred_at=None):
        windows = p.get("substitution_windows")
        if not windows:
            tip = parse_ts(p["tipoff_at"])
            windows = [{"opens_at": (tip - timedelta(hours=1)).isoformat(),
                        "closes_at": p["tipoff_at"], "reason": "any"}]
        for w in windows:
            if w["reason"] not in ("any", "medical"):
                raise DomainError("替补窗口类型仅支持 any / medical")
            if parse_ts(w["closes_at"]) <= parse_ts(w["opens_at"]):
                raise DomainError("替补窗口结束时间必须晚于开始时间")
        p["substitution_windows"] = windows
        return self.ledger.append("GameScheduled", actor, p, occurred_at=occurred_at)

    def cmd_assign_game_staff(self, actor, p, *, occurred_at=None):
        """当场工作人员名单（随比赛日推进可重新指派，每次为完整快照）。"""
        self._require_game(p["game_id"])
        for item in p["assignments"]:
            self._require_person(item["person_id"])
        return self.ledger.append("GameStaffAssigned", actor, p, occurred_at=occurred_at)

    def cmd_book_medical_resource(self, actor, p, *, occurred_at=None):
        """医疗资源保障：驻场医疗官、设备、救护车时段。"""
        self._require_range(p)
        return self.ledger.append("MedicalResourceBooked", actor, p, occurred_at=occurred_at)

    # ----- 票选名单（名单基线） -----
    def cmd_publish_vote_roster(self, actor, p, *, occurred_at=None):
        self._require_game(p["game_id"])
        for pid in p["player_ids"]:
            self._require_person(pid)
        payload = {"version": self._next_version("vote", p["game_id"]), **p}
        return self.ledger.append("VoteRosterPublished", actor, payload, occurred_at=occurred_at)

    # ----- 替补：申请 → 批准 → 窗口内生效 -----
    def cmd_request_substitution(self, actor, p, *, occurred_at=None):
        self._require_game(p["game_id"])
        self._require_person(p["in_player_id"])
        if not p.get("reason"):
            raise DomainError("替补申请必须说明原因")
        if not p.get("basis"):
            raise DomainError("替补申请必须附依据（医疗诊断/缺席证明编号）")
        if self._correlation_exists(p["request_id"]):
            raise DomainError(f"替补申请 {p['request_id']} 已存在")
        return self.ledger.append(
            "SubstitutionRequested", actor, p,
            occurred_at=occurred_at, correlation_id=p["request_id"],
        )

    def cmd_approve_substitution(self, actor, p, *, occurred_at=None):
        request = self._find_correlation(p["request_id"], "SubstitutionRequested")
        if request is None:
            raise DomainError("替补申请不存在")
        if self._correlation_has(p["request_id"], "SubstitutionApproved"):
            raise DomainError("该申请已批准，不得重复批准")
        if actor not in self._approvers():
            raise DomainError("只有协会或队伍负责人可批准替补")
        if not p.get("basis"):
            raise DomainError("批准必须记录规则依据")
        return self.ledger.append(
            "SubstitutionApproved", actor,
            {"request_id": p["request_id"], "basis": p["basis"],
             "approver_role": p.get("approver_role", actor)},
            occurred_at=occurred_at, correlation_id=p["request_id"],
        )

    def cmd_activate_substitution(self, actor, p, *, occurred_at=None, idempotency_key=None):
        """替补在此时真正进入当场名单——必须落在规则窗口内。"""
        request = self._find_correlation(p["request_id"], "SubstitutionRequested")
        approval = self._find_correlation(p["request_id"], "SubstitutionApproved")
        if request is None or approval is None:
            raise DomainError("替补须先申请并获批准")
        at = parse_ts(occurred_at or now_ts())
        game = self._require_game(request.payload["game_id"])
        window = self._matching_window(game, request.payload["reason"], at)
        if window is None:
            raise DomainError(
                f"当前时间不在允许的替补窗口内（{request.payload['reason']}）"
            )
        return self.ledger.append(
            "SubstitutionActivated", actor,
            {"request_id": p["request_id"], "game_id": request.payload["game_id"],
             "out_player_id": request.payload["out_player_id"],
             "in_player_id": request.payload["in_player_id"],
             "reason": request.payload["reason"],
             "approved_by": approval.actor, "approval_basis": approval.payload["basis"],
             "window": window},
            occurred_at=occurred_at, correlation_id=p["request_id"],
            idempotency_key=idempotency_key,
        )

    # ----- 现场核验（设备失联 → 补传一次登记） -----
    def cmd_verification_checkin(self, actor, p, *, occurred_at=None, idempotency_key=None):
        self._require_person(p["person_id"])
        if not idempotency_key:
            raise DomainError("核验登记必须带设备登记号（幂等键）")
        game = self._require_game(p["game_id"])
        eligibility = self.explain_eligibility(p["person_id"], p["game_id"], occurred_at or now_ts())
        if not eligibility["eligible"]:
            raise DomainError("资格核验不通过: " + "；".join(eligibility["reasons"]))
        return self.ledger.append(
            "VerificationCheckin", actor,
            {**p, "device_id": p.get("device_id", actor),
             "result": "passed", "eligibility_snapshot": eligibility},
            occurred_at=occurred_at, idempotency_key=idempotency_key,
        )

    # ----- 多语言通知（每版独立，逐人送达回执） -----
    def cmd_issue_notice(self, actor, p, *, occurred_at=None):
        if not p.get("template") or not p.get("audience"):
            raise DomainError("通知缺少模板或受众")
        payload = {"version": self._next_version("notice", p["notice_id"]), **p}
        return self.ledger.append("NoticeIssued", actor, payload, occurred_at=occurred_at)

    def cmd_record_delivery(self, actor, p, *, occurred_at=None, idempotency_key=None):
        notice = None
        for e in self.ledger.events(type_in={"NoticeIssued"}):
            if e.payload["notice_id"] == p["notice_id"] and e.payload["version"] == p["version"]:
                notice = e
        if notice is None:
            raise DomainError("通知版本不存在")
        if p["person_id"] not in notice.payload["audience"]:
            raise DomainError("该人员不在此版通知受众范围内")
        key = idempotency_key or f"dlv:{p['notice_id']}:v{p['version']}:{p['person_id']}:{p['channel']}"
        return self.ledger.append(
            "NoticeDelivered", actor, p, occurred_at=occurred_at, idempotency_key=key,
        )

    # ----- 医疗处置与费用分摊 -----
    def cmd_report_medical_incident(self, actor, p, *, occurred_at=None, idempotency_key=None):
        self._require_game(p["game_id"])
        self._require_person(p["person_id"])
        return self.ledger.append(
            "MedicalIncidentOccurred", actor, p,
            occurred_at=occurred_at, idempotency_key=idempotency_key,
        )

    def cmd_update_medical_disposition(self, actor, p, *, occurred_at=None):
        """处置进展以新版本追加，现场首报不被覆盖。"""
        incident = self._find_by_payload("incident_id", p["incident_id"], {"MedicalIncidentOccurred"})
        if incident is None:
            raise DomainError("医疗事件不存在")
        payload = {"version": self._next_version("medical", p["incident_id"]), **p}
        return self.ledger.append("MedicalDispositionRecorded", actor, payload, occurred_at=occurred_at)

    def cmd_allocate_expense(self, actor, p, *, occurred_at=None):
        total = sum(s["amount"] for s in p["splits"])
        if abs(total - p["amount"]) > 0.005:
            raise DomainError(f"费用分摊合计 {total} 与金额 {p['amount']} 不一致")
        return self.ledger.append("ExpenseAllocated", actor, p, occurred_at=occurred_at)

    # ----- 裁判记录 / 技术统计 / 比分：原始件不可变，修正以新版本生效 -----
    def cmd_submit_referee_record(self, actor, p, *, occurred_at=None, idempotency_key=None):
        self._require_game(p["game_id"])
        if self.ledger.events(
            type_in={"RefereeRecordSubmitted"}, aggregate_field=("record_id", p["record_id"])
        ):
            raise DomainError("裁判原始记录已存在，不可覆盖")
        payload = {"version": 1, **p}
        return self.ledger.append(
            "RefereeRecordSubmitted", actor, payload,
            occurred_at=occurred_at, idempotency_key=idempotency_key,
        )

    def cmd_propose_stat_correction(self, actor, p, *, occurred_at=None):
        record = self._latest_referee_version(p["record_id"])
        if record is None:
            raise DomainError("裁判记录不存在")
        if not p.get("changes"):
            raise DomainError("修正必须给出字段变更")
        payload = {"status": "proposed", "version": record["version"] + 1, **p}
        return self.ledger.append("StatCorrectionProposed", actor, payload, occurred_at=occurred_at)

    def cmd_decide_stat_correction(self, actor, p, *, occurred_at=None):
        proposal = self._find_by_payload("correction_id", p["correction_id"], {"StatCorrectionProposed"})
        if proposal is None:
            raise DomainError("修正申请不存在")
        if proposal.payload["status"] != "proposed":
            raise DomainError("该修正已裁定")
        if p["decision"] not in ("approved", "rejected"):
            raise DomainError("裁定结果仅支持 approved / rejected")
        if p["decision"] == "approved" and not p.get("basis"):
            raise DomainError("批准统计修正必须记录依据")
        decided = self.ledger.append(
            "StatCorrectionDecided", actor,
            {**p, "record_id": proposal.payload["record_id"],
             "game_id": proposal.payload["game_id"],
             "new_version": proposal.payload["version"]},
            occurred_at=occurred_at,
        )
        if p["decision"] == "approved":
            latest = self._latest_referee_version(proposal.payload["record_id"])
            new_snapshot = {**latest["snapshot"], **proposal.payload["changes"]}
            self.ledger.append(
                "RefereeRecordVersionPublished", actor,
                {"record_id": proposal.payload["record_id"],
                 "game_id": proposal.payload["game_id"],
                 "version": proposal.payload["version"],
                 "parent_version": latest["version"],
                 "snapshot": new_snapshot,
                 "correction_id": p["correction_id"],
                 "basis": p["basis"], "decided_by": actor},
                occurred_at=occurred_at,
            )
        return decided

    def cmd_post_score(self, actor, p, *, occurred_at=None):
        self._require_game(p["game_id"])
        payload = {"version": 1, **p}
        return self.ledger.append("ScorePosted", actor, payload, occurred_at=occurred_at)

    def cmd_file_score_appeal(self, actor, p, *, occurred_at=None):
        self._require_game(p["game_id"])
        if not p.get("basis"):
            raise DomainError("比分申诉必须附依据")
        return self.ledger.append("ScoreAppealFiled", actor, p, occurred_at=occurred_at)

    def cmd_decide_score_appeal(self, actor, p, *, occurred_at=None):
        appeal = self._find_by_payload("appeal_id", p["appeal_id"], {"ScoreAppealFiled"})
        if appeal is None:
            raise DomainError("申诉不存在")
        if p["decision"] not in ("approved", "rejected"):
            raise DomainError("裁定结果仅支持 approved / rejected")
        decided = self.ledger.append(
            "ScoreAppealDecided", actor,
            {**p, "game_id": appeal.payload["game_id"], "decided_by": actor},
            occurred_at=occurred_at,
        )
        if p["decision"] == "approved":
            self.ledger.append(
                "ScoreRevisionPublished", actor,
                {"game_id": appeal.payload["game_id"], "appeal_id": p["appeal_id"],
                 "version": self._next_version("score", appeal.payload["game_id"]),
                 "score": p["score"], "basis": p.get("basis", "申诉成立"),
                 "decided_by": actor},
                occurred_at=occurred_at,
            )
        return decided

    def cmd_order_replay(self, actor, p, *, occurred_at=None):
        """加时赛统计争议等导致重赛：原比赛记录保留，另立重赛场次。"""
        self._require_game(p["game_id"])
        if not p.get("new_game_id") or not p.get("basis"):
            raise DomainError("重赛裁定必须指明新场次与依据")
        return self.ledger.append("ReplayOrdered", actor, p, occurred_at=occurred_at)

    # ===== 查询（均可 at= 回到任意时刻） =====

    def effective_roster(self, game_id: str, at: Optional[str] = None) -> dict[str, Any]:
        """当场在 at 时刻有效的球员名单与工作人员。"""
        game = self._require_game(game_id)
        player_ids, substitutions, vote_version = self._roster_player_ids(game_id, at)
        staff = None
        for e in self.ledger.events(type_in={"GameStaffAssigned"}, at=at):
            if e.payload["game_id"] == game_id:
                staff = e.payload["assignments"]
        return {
            "game_id": game_id,
            "as_of": at,
            "based_on_vote_version": vote_version,
            "players": [self._player_summary(pid, game_id, at) for pid in player_ids],
            "staff": staff or [],
            "substitutions_applied": substitutions,
        }

    def _roster_player_ids(self, game_id: str, at: Optional[str] = None) -> tuple[list[str], list[dict], Optional[int]]:
        """投影：at 时刻当场名单上的球员 ID 列表、生效的替补链与票选基线版本。"""
        vote = None
        for e in self.ledger.events(type_in={"VoteRosterPublished"}, at=at):
            if e.payload["game_id"] == game_id:
                vote = e
        player_ids = list(vote.payload["player_ids"]) if vote else []
        substitutions = []
        for e in self.ledger.events(type_in={"SubstitutionActivated"}, at=at):
            if e.payload["game_id"] != game_id:
                continue
            if e.payload["out_player_id"] in player_ids:
                player_ids.remove(e.payload["out_player_id"])
            if e.payload["in_player_id"] not in player_ids:
                player_ids.append(e.payload["in_player_id"])
            substitutions.append(e.payload)
        return player_ids, substitutions, vote.payload["version"] if vote else None

    def explain_eligibility(self, person_id: str, game_id: str, at: str) -> dict[str, Any]:
        """解释某人在 at 时刻为何有/无参赛资格。"""
        person = self._persons().get(person_id)
        reasons = []
        if person is None:
            return {"person_id": person_id, "eligible": False, "reasons": ["人员未登记"]}
        cred = self._effective_credential(person_id, at)
        if cred is None:
            reasons.append("无有效通行证件（缺失或已过期）")
        insurance = self._effective_insurance(person_id, at)
        if insurance is None:
            reasons.append("无有效保险（缺失或已过期）")
        if person["role"] == "player" and person_id not in self._roster_player_ids(game_id, at)[0]:            reasons.append("不在当场有效名单内（票选名单或已批准替补）")
        return {"person_id": person_id, "eligible": not reasons,
                "reasons": reasons,
                "credential_valid_until": cred and cred["valid_until"],
                "insurance_valid_until": insurance and insurance["valid_until"]}

    def person_view(self, viewer_id: str, person_id: str, at: Optional[str] = None) -> dict[str, Any]:
        """按管理员职责（属地 + 字段级）脱敏后的个人视图。"""
        viewer = self._admins().get(viewer_id)
        if viewer is None:
            raise DomainError("查看者未登记或无账户")
        person = self._persons().get(person_id)
        if person is None:
            raise DomainError("人员不存在")
        if viewer["role"] != ROLE_ASSOCIATION:
            if person["region"] not in viewer.get("regions", []) and person["team_id"] not in viewer.get("team_ids", []):
                raise DomainError("跨地区查阅被拒绝：该人员不在你的职责范围内")
        grants = ROLE_FIELD_GRANTS[viewer["role"]]
        safe = {"person_id": person_id, "name": person["name"], "role": person["role"],
                "team_id": person["team_id"], "region": person["region"]}
        if person.get("jersey_number") is not None:
            safe["jersey_number"] = person["jersey_number"]
        for key, value in (person.get("pii") or {}).items():
            level = FIELD_LEVELS.get(key, "sensitive")
            if level in grants:
                safe[key] = value
        if "document" in grants:
            cred = self._effective_credential(person_id, at or now_ts())
            if cred:
                safe["credential"] = cred
        else:
            cred = self._effective_credential(person_id, at or now_ts())
            safe["credential_status"] = "valid" if cred else "missing_or_expired"
        if "document" in grants:
            ins = self._effective_insurance(person_id, at or now_ts())
            if ins:
                safe["insurance"] = ins
        else:
            ins = self._effective_insurance(person_id, at or now_ts())
            safe["insurance_status"] = "valid" if ins else "missing_or_expired"
        return {"viewer_id": viewer_id, "viewer_role": viewer["role"], "as_of": at, "data": safe}

    def notice_status(self, notice_id: str) -> dict[str, Any]:
        versions = []
        for e in self.ledger.events(type_in={"NoticeIssued"}):
            if e.payload["notice_id"] != notice_id:
                continue
            receipts = [
                {"person_id": d.payload["person_id"], "channel": d.payload["channel"],
                 "language": d.payload.get("language"), "status": d.payload["status"],
                 "at": d.occurred_at}
                for d in self.ledger.events(type_in={"NoticeDelivered"})
                if d.payload["notice_id"] == notice_id and d.payload["version"] == e.payload["version"]
            ]
            audience = e.payload["audience"]
            delivered = {r["person_id"] for r in receipts if r["status"] in ("delivered", "read")}
            versions.append({
                "version": e.payload["version"], "issued_at": e.occurred_at,
                "languages": e.payload.get("languages"), "channels": e.payload.get("channels"),
                "audience_size": len(audience),
                "delivered_count": len(delivered),
                "pending": [pid for pid in audience if pid not in delivered],
                "receipts": receipts,
            })
        return {"notice_id": notice_id, "versions": versions}

    def medical_view(self, game_id: str, at: Optional[str] = None) -> dict[str, Any]:
        incidents = []
        for e in self.ledger.events(type_in={"MedicalIncidentOccurred"}, at=at):
            if e.payload["game_id"] != game_id:
                continue
            updates = [
                {"version": u.payload["version"], "at": u.occurred_at, "by": u.actor,
                 "disposition": u.payload["disposition"]}
                for u in self.ledger.events(type_in={"MedicalDispositionRecorded"}, at=at)
                if u.payload["incident_id"] == e.payload["incident_id"]
            ]
            incidents.append({"incident_id": e.payload["incident_id"],
                              "reported_at": e.occurred_at, "reported_by": e.actor,
                              "person_id": e.payload["person_id"],
                              "initial_report": e.payload, "updates": updates})
        resources = [e.payload for e in self.ledger.events(type_in={"MedicalResourceBooked"}, at=at)
                     if e.payload.get("game_id") == game_id]
        return {"game_id": game_id, "as_of": at,
                "resources": resources, "incidents": incidents}

    def expenses(self, game_id: str, at: Optional[str] = None) -> dict[str, Any]:
        items = [e.payload for e in self.ledger.events(type_in={"ExpenseAllocated"}, at=at)
                 if e.payload["game_id"] == game_id]
        totals: dict[str, float] = defaultdict(float)
        for item in items:
            for split in item["splits"]:
                totals[split["party"]] += split["amount"]
        return {"game_id": game_id, "as_of": at, "items": items,
                "party_totals": dict(totals)}

    def roster_change_history(self, game_id: str) -> dict[str, Any]:
        """完整解释名单变化：票选基线 + 每条替补的申请人/批准人/依据/窗口。"""
        changes = []
        for e in self.ledger.events(type_in={"VoteRosterPublished"}):
            if e.payload["game_id"] == game_id:
                changes.append({
                    "kind": "vote_roster", "at": e.occurred_at, "by": e.actor,
                    "version": e.payload["version"],
                    "basis": f"票选名单 {e.payload.get('vote_ref', '')}".strip(),
                    "player_ids": e.payload["player_ids"],
                })
        requests = {e.payload["request_id"]: e
                    for e in self.ledger.events(type_in={"SubstitutionRequested"})
                    if e.payload["game_id"] == game_id}
        for rid, req in requests.items():
            approval = self._find_correlation(rid, "SubstitutionApproved")
            activation = self._find_correlation(rid, "SubstitutionActivated")
            changes.append({
                "kind": "substitution", "request_id": rid,
                "requested_at": req.occurred_at, "requested_by": req.actor,
                "out_player_id": req.payload["out_player_id"],
                "in_player_id": req.payload["in_player_id"],
                "reason": req.payload["reason"], "request_basis": req.payload["basis"],
                "approved_at": approval.occurred_at if approval else None,
                "approved_by": approval.actor if approval else None,
                "approval_basis": approval.payload["basis"] if approval else None,
                "activated_at": activation.occurred_at if activation else None,
                "window": activation.payload["window"] if activation else None,
                "status": "activated" if activation else ("approved" if approval else "requested"),
            })
        return {"game_id": game_id, "changes": changes}

    def referee_record_versions(self, record_id: str) -> dict[str, Any]:
        """裁判原始记录（不可变）与历次修正后的新版本链。"""
        versions = []
        for e in self.ledger.events(type_in={"RefereeRecordSubmitted"}):
            if e.payload["record_id"] == record_id:
                versions.append({"version": 1, "snapshot": e.payload["snapshot"],
                                 "submitted_at": e.occurred_at, "submitted_by": e.actor,
                                 "note": "裁判原始记录（不可修改）"})
        for e in self.ledger.events(type_in={"RefereeRecordVersionPublished"}):
            if e.payload["record_id"] == record_id:
                versions.append({"version": e.payload["version"],
                                 "parent_version": e.payload["parent_version"],
                                 "snapshot": e.payload["snapshot"],
                                 "published_at": e.occurred_at, "published_by": e.actor,
                                 "correction_id": e.payload["correction_id"],
                                 "basis": e.payload["basis"]})
        decisions = [
            {"correction_id": d.payload["correction_id"], "decision": d.payload["decision"],
             "by": d.actor, "at": d.occurred_at,
             "basis": d.payload.get("basis")}
            for d in self.ledger.events(type_in={"StatCorrectionDecided"})
            if d.payload["record_id"] == record_id
        ]
        return {"record_id": record_id, "current_version": versions[-1]["version"] if versions else None,
                "versions": versions, "decisions": decisions}

    def game_score_history(self, game_id: str) -> dict[str, Any]:
        posted = [e for e in self.ledger.events(type_in={"ScorePosted"})
                  if e.payload["game_id"] == game_id]
        revisions = [e for e in self.ledger.events(type_in={"ScoreRevisionPublished"})
                     if e.payload["game_id"] == game_id]
        appeals = []
        for e in self.ledger.events(type_in={"ScoreAppealFiled", "ScoreAppealDecided"}):
            if e.payload["game_id"] == game_id:
                appeals.append({"type": e.type, "at": e.occurred_at, "by": e.actor, **e.payload})
        current = revisions[-1].payload if revisions else (posted[-1].payload if posted else None)
        return {"game_id": game_id, "original_score": posted[0].payload["score"] if posted else None,
                "current": current, "appeals": appeals}

    def game_postmortem(self, game_id: str, at: Optional[str] = None) -> dict[str, Any]:
        """赛后一站式归档：从任意一场比赛进入，回到任意时刻。"""
        game = self._require_game(game_id)
        replays = [{"by": e.actor, "at": e.occurred_at, **e.payload}
                   for e in self.ledger.events(type_in={"ReplayOrdered"})
                   if e.payload["game_id"] == game_id or e.payload["new_game_id"] == game_id]
        return {
            "game": game.payload,
            "as_of": at,
            "roster": self.effective_roster(game_id, at),
            "roster_history": self.roster_change_history(game_id),
            "medical": self.medical_view(game_id, at),
            "expenses": self.expenses(game_id, at),
            "score": self.game_score_history(game_id),
            "replays": replays,
            "notices": self._game_notices(game_id),
        }

    def verification_journal(self, game_id: str) -> dict[str, Any]:
        entries = []
        for e in self.ledger.events(type_in={"VerificationCheckin"}):
            if e.payload["game_id"] != game_id:
                continue
            entries.append({"at": e.occurred_at, "ingested_at": e.ingested_at,
                            "device_id": e.payload["device_id"],
                            "registration_no": e.idempotency_key,
                            "person_id": e.payload["person_id"],
                            "late_reupload": e.ingested_at != e.occurred_at,
                            "result": e.payload["result"]})
        return {"game_id": game_id, "entries": entries, "count": len(entries)}

    # ===== 内部投影 =====

    def _persons(self) -> dict[str, dict]:
        result = {}
        for e in self.ledger.events(type_in={"PersonRegistered"}):
            result[e.payload["person_id"]] = e.payload
        return result

    def _admins(self) -> dict[str, dict]:
        result = {}
        for e in self.ledger.events(type_in={"AdminRegistered"}):
            result[e.payload["admin_id"]] = e.payload
        return result

    def _effective_credential(self, person_id: str, at: str) -> Optional[dict]:
        t = parse_ts(at)
        current = None
        for e in self.ledger.events(type_in={"CredentialRecorded"}, at=at):
            if e.payload["person_id"] != person_id:
                continue
            if parse_ts(e.payload["valid_from"]) <= t <= parse_ts(e.payload["valid_until"]):
                current = e.payload
        return current

    def _effective_insurance(self, person_id: str, at: str) -> Optional[dict]:
        t = parse_ts(at)
        person = self._persons().get(person_id)
        current = None
        for e in self.ledger.events(type_in={"InsuranceBound"}, at=at):
            p = e.payload
            covers_person = (
                p.get("person_id") == person_id
                or (person and p.get("team_id") == person["team_id"]
                    and (not p.get("covered") or person_id in p["covered"]))
            )
            if covers_person and parse_ts(p["valid_from"]) <= t <= parse_ts(p["valid_until"]):
                current = p
        return current

    def _player_summary(self, person_id: str, game_id: str, at: Optional[str]) -> dict[str, Any]:
        person = self._persons()[person_id]
        summary = {"person_id": person_id, "name": person["name"],
                   "jersey_number": person.get("jersey_number")}
        if at:
            elig = self.explain_eligibility(person_id, game_id, at)
            summary["eligible"] = elig["eligible"]
            summary["reasons"] = elig["reasons"]
        else:
            summary["eligible"] = None
            summary["reasons"] = []
        return summary

    def _latest_referee_version(self, record_id: str) -> Optional[dict]:
        versions = self.referee_record_versions(record_id)["versions"]
        return versions[-1] if versions else None

    def _game_notices(self, game_id: str) -> list[dict[str, Any]]:
        out = []
        for e in self.ledger.events(type_in={"NoticeIssued"}):
            if e.payload.get("game_id") == game_id:
                status = self.notice_status(e.payload["notice_id"])
                out.append(next(v for v in status["versions"]
                                if v["version"] == e.payload["version"]))
        return out

    # ===== 小工具 =====

    def _require_person(self, person_id: str) -> dict:
        person = self._persons().get(person_id)
        if person is None:
            raise DomainError(f"人员未登记: {person_id}")
        return person

    def _require_game(self, game_id: str):
        for e in self.ledger.events(type_in={"GameScheduled"}):
            if e.payload["game_id"] == game_id:
                return e
        raise DomainError(f"比赛未编排: {game_id}")

    @staticmethod
    def _require_validity(p: dict) -> None:
        for key in ("valid_from", "valid_until"):
            if not p.get(key):
                raise DomainError(f"缺少有效期字段: {key}")
        if parse_ts(p["valid_until"]) <= parse_ts(p["valid_from"]):
            raise DomainError("有效期结束必须晚于开始")

    @staticmethod
    def _require_range(p: dict) -> None:
        if not p.get("starts_at") or not p.get("ends_at"):
            raise DomainError("缺少时段起止时间")
        if parse_ts(p["ends_at"]) <= parse_ts(p["starts_at"]):
            raise DomainError("时段结束必须晚于开始")

    @staticmethod
    def _ranges_overlap(a_start, a_end, b_start, b_end) -> bool:
        return parse_ts(a_start) < parse_ts(b_end) and parse_ts(b_start) < parse_ts(a_end)

    def _matching_window(self, game, reason: str, at: datetime) -> Optional[dict]:
        """窗口与替补原因双向匹配：赛前 any 窗口只受理常规替换，
        赛中 medical 窗口只受理伤病替换。"""
        is_medical = reason in SUB_MEDICAL_REASONS
        for window in game.payload["substitution_windows"]:
            if not (parse_ts(window["opens_at"]) <= at <= parse_ts(window["closes_at"])):
                continue
            if window["reason"] == "medical" and not is_medical:
                continue
            if window["reason"] == "any" and is_medical:
                continue
            return window
        return None

    def _approvers(self) -> set[str]:
        return {pid for pid, a in self._admins().items()
                if a["role"] in (ROLE_ASSOCIATION, ROLE_TEAM)} | {"committee"}

    def _find_correlation(self, request_id: str, event_type: str):
        for e in self.ledger.events(type_in={event_type}):
            if e.correlation_id == request_id or e.payload.get("request_id") == request_id:
                return e
        return None

    def _correlation_exists(self, request_id: str) -> bool:
        return self._find_correlation(request_id, "SubstitutionRequested") is not None

    def _correlation_has(self, request_id: str, event_type: str) -> bool:
        return self._find_correlation(request_id, event_type) is not None

    def _find_by_payload(self, key: str, value: str, types: set[str]):
        for e in self.ledger.events(type_in=types):
            if e.payload.get(key) == value:
                return e
        return None

    def _next_version(self, kind: str, key: str) -> int:
        type_map = {
            "vote": "VoteRosterPublished",
            "notice": "NoticeIssued",
            "medical": "MedicalDispositionRecorded",
            "score": "ScoreRevisionPublished",
        }
        field = {"vote": "game_id", "notice": "notice_id",
                 "medical": "incident_id", "score": "game_id"}[kind]
        count = sum(1 for e in self.ledger.events(type_in={type_map[kind]})
                    if e.payload.get(field) == key)
        return count + 1
