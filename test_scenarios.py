"""端到端场景测试：两天赛程内的资格、窗口、补传、版本、通知、医疗与权限。"""

import os
import tempfile
import unittest

from baysports.clock import FixedClock
from baysports.errors import (
    AuthorizationError, ConflictError, NotFoundError, RuleViolationError,
)
from baysports.store import EventStore
from baysports.tournament import Tournament
from baysports import views
from baysports.auth import Viewer, authorize, person_view

# 两天赛程时间线（UTC）
D1 = "2026-09-19T09:00:00Z"          # 注册日
TIPOFF = "2026-09-20T12:00:00Z"      # 比赛开球
Q1_END = "2026-09-20T12:20:00Z"
FULL_TIME = "2026-09-20T13:50:00Z"   # 加时后完赛
DOC_UNTIL = "2026-09-25T23:59:00Z"
INS_UNTIL = "2026-09-22T23:59:00Z"
GRANT_FROM, GRANT_UNTIL = "2026-09-19T00:00:00Z", "2026-09-21T23:59:00Z"


def make_world(start=D1):
    clock = FixedClock(start)
    store = EventStore(clock)
    t = Tournament(store, clock)
    return clock, store, t


def register_ready_player(t, pid, team, region, name=None, jersey="10",
                          doc_until=DOC_UNTIL, ins_until=INS_UNTIL,
                          actor="sec-gd-01"):
    """注册一名材料齐全、当前有资格的球员。"""
    t.register_person(pid, team, name or pid, region, "player",
                      jersey_no=jersey,
                      contacts={"phone": f"1390000{pid[-2:]}",
                                "email": f"{pid.lower()}@example.org"},
                      languages=["zh-HK" if region == "HK" else "zh-CN", "en"],
                      emergency_contact={"name": "家属", "phone": "999"},
                      actor=actor)
    t.record_travel_document(pid, "港澳通行证", f"H{pid}1234567",
                             "2026-09-01T00:00:00Z", doc_until, actor)
    t.record_insurance(pid, f"POL-{pid}", "湾区联保",
                       "2026-09-01T00:00:00Z", ins_until,
                       {"medical": 500000}, actor)
    t.grant_eligibility(pid, "asg_participation", GRANT_FROM, GRANT_UNTIL,
                        {"note": "全明星参赛确认"}, actor)
    return pid


class ScenarioSetUp:
    def setUp(self):  # noqa: N802
        self.clock, self.store, self.t = make_world()
        t = self.t
        t.register_team("T-HK", "香港湾区队", "HK", "sec-hk-01")
        t.register_team("T-GD", "广东南粤队", "GD", "sec-gd-01")
        # 三名香港球员：01 正选、02 替补（票选池内）、03 通行材料过期
        register_ready_player(t, "P-HK-01", "T-HK", "HK", jersey="7")
        register_ready_player(t, "P-HK-02", "T-HK", "HK", jersey="11")
        register_ready_player(t, "P-HK-03", "T-HK", "HK", jersey="23",
                              doc_until="2026-09-10T00:00:00Z")
        register_ready_player(t, "P-GD-01", "T-GD", "GD", jersey="9")
        # 教练同样持有通行材料、保险与参赛资格（名单对工作人员同样校验）
        t.register_person("S-HK-01", "T-HK", "港队教练", "HK", "staff",
                          contacts={"phone": "13900000011"},
                          languages=["zh-HK", "en"], actor="sec-hk-01")
        t.record_travel_document("S-HK-01", "港澳通行证", "HS011234567",
                                 "2026-09-01T00:00:00Z", DOC_UNTIL, "sec-hk-01")
        t.record_insurance("S-HK-01", "POL-S01", "湾区联保",
                           "2026-09-01T00:00:00Z", INS_UNTIL,
                           {"medical": 500000}, "sec-hk-01")
        t.grant_eligibility("S-HK-01", "asg_participation", GRANT_FROM,
                            GRANT_UNTIL, {"note": "教练注册确认"}, "sec-chief-01")
        # 票选与确认
        for pid, votes in (("P-HK-01", 9800), ("P-HK-02", 8700),
                           ("P-HK-03", 1200), ("P-GD-01", 9100)):
            t.nominate(pid, votes, "ballot-system")
        t.confirm_ballot(["P-HK-01", "P-HK-02", "P-GD-01"], "sec-chief-01")
        # 场馆时段与比赛
        t.register_venue_slot(
            "SLOT-OPEN", "湾区体育馆A", "GD",
            "2026-09-20T09:00:00Z", "2026-09-20T15:00:00Z",
            "全明星正赛", capacity=200, actor="venue-gd-01")
        t.register_venue_slot(
            "SLOT-TRAIN", "训练馆B", "GD",
            "2026-09-19T16:00:00Z", "2026-09-19T18:00:00Z",
            "适应性训练", capacity=40, actor="venue-gd-01")
        t.schedule_game("G1", "粤BA全明星正赛", "SLOT-OPEN", TIPOFF,
                        ["T-HK", "T-GD"], "sec-chief-01")


class EligibilityTest(ScenarioSetUp, unittest.TestCase):
    def test_expired_travel_document_blocks_roster(self):
        with self.assertRaises(RuleViolationError) as ctx:
            self.t.submit_roster(
                "G1", ["P-HK-01", "P-HK-03"], ["S-HK-01"], "sec-hk-01",
                at="2026-09-20T10:00:00Z")
        self.assertEqual(ctx.exception.details["failed"], ["travel_document"])

    def test_insurance_must_cover_day_after_game(self):
        # 保险在比赛当天到期，未覆盖赛后天数 -> 拒绝
        self.t.register_person("P-HK-09", "T-HK", "短保险球员", "HK",
                               "player", actor="sec-hk-01")
        self.t.record_travel_document("P-HK-09", "港澳通行证", "H9009",
                                      "2026-09-01T00:00:00Z", DOC_UNTIL,
                                      "sec-hk-01")
        self.t.record_insurance("P-HK-09", "POL9", "联保",
                                "2026-09-01T00:00:00Z",
                                "2026-09-20T18:00:00Z", {}, "sec-hk-01")
        self.t.grant_eligibility("P-HK-09", "asg_participation",
                                 GRANT_FROM, GRANT_UNTIL, {}, "sec-chief-01")
        with self.assertRaises(RuleViolationError) as ctx:
            self.t.submit_roster("G1", ["P-HK-09"], [], "sec-hk-01",
                                 at="2026-09-20T10:00:00Z")
        self.assertEqual(ctx.exception.details["failed"], ["insurance"])

    def test_grant_outside_validity_window_blocks_roster(self):
        # 资格在开球次日已失效：资格检查直接反映有效期
        from baysports.tournament import person_eligibility_at
        person = views.build_person(self.store, "P-HK-01")
        at_tipoff = person_eligibility_at(person, TIPOFF)
        self.assertTrue(at_tipoff["eligibility_grant"])
        after_expiry = person_eligibility_at(person, "2026-09-22T12:00:00Z")
        self.assertFalse(after_expiry["eligibility_grant"])

    def test_valid_roster_submitted_before_window(self):
        e = self.t.submit_roster(
            "G1", ["P-HK-01", "P-GD-01"], ["S-HK-01"], "sec-hk-01",
            at="2026-09-20T10:00:00Z")
        self.assertEqual(e.etype, "RosterSubmitted")


class RosterWindowTest(ScenarioSetUp, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.t.submit_roster(
            "G1", ["P-HK-01", "P-GD-01"], ["S-HK-01"], "sec-hk-01",
            at="2026-09-20T10:00:00Z")

    def test_normal_change_after_deadline_rejected(self):
        self.t.request_roster_change(
            "RC-1", "G1", "P-HK-02", "P-HK-01", "normal", "",
            "coach-hk", at="2026-09-20T10:40:00Z")
        with self.assertRaises(RuleViolationError):
            self.t.approve_roster_change(
                "RC-1", "sec-chief-01", "secretariat_chief", "常规换人",
                at="2026-09-20T10:40:00Z")

    def test_medical_change_flow(self):
        with self.assertRaises(RuleViolationError):
            self.t.request_roster_change(
                "RC-2", "G1", "P-HK-02", "P-HK-01", "medical", "",
                "coach-hk", at="2026-09-20T11:30:00Z")
        self.t.request_roster_change(
            "RC-3", "G1", "P-HK-02", "P-HK-01", "medical", "MED-CERT-77",
            "coach-hk", at="2026-09-20T11:30:00Z")
        # 场地管理员无权批准
        with self.assertRaises(RuleViolationError) as ctx:
            self.t.approve_roster_change(
                "RC-3", "venue-gd-01", "venue_marshal", "医疗替补",
                at="2026-09-20T11:35:00Z")
        self.assertIn("无权批准", ctx.exception.message)
        # 医疗官在开球前15分钟窗口内批准成功
        e = self.t.approve_roster_change(
            "RC-3", "med-officer-01", "medical_officer",
            "凭MED-CERT-77号医疗证明", at="2026-09-20T11:40:00Z")
        self.assertEqual(e.data["status"], "approved")

    def test_emergency_only_after_tipoff_and_before_q1_end(self):
        self.t.request_roster_change(
            "RC-4", "G1", "P-HK-02", "P-GD-01", "emergency", "INCIDENT-9",
            "referee-01", at=TIPOFF)
        # 开球前不得走紧急窗口
        with self.assertRaises(RuleViolationError):
            self.t.approve_roster_change(
                "RC-4", "referee-01", "referee", "突发不到场",
                at="2026-09-20T11:55:00Z")
        # 第一节结束后关闭
        with self.assertRaises(RuleViolationError):
            self.t.approve_roster_change(
                "RC-4", "referee-01", "referee", "突发不到场",
                at="2026-09-20T12:25:00Z")
        # 窗口内成功
        self.t.approve_roster_change(
            "RC-4", "referee-01", "referee", "球员开场受伤",
            at="2026-09-20T12:10:00Z")

    def test_substitute_must_come_from_confirmed_ballot(self):
        # P-HK-03 提名过但未被确认进票选名单
        with self.assertRaises(RuleViolationError) as ctx:
            self.t.request_roster_change(
                "RC-5", "G1", "P-HK-03", "P-HK-01", "normal", "",
                "coach-hk", at="2026-09-20T10:05:00Z")
        self.assertEqual(ctx.exception.details["person_id"], "P-HK-03")

    def test_double_approval_rejected(self):
        self.t.request_roster_change(
            "RC-6", "G1", "P-HK-02", "P-HK-01", "normal", "",
            "coach-hk", at="2026-09-20T10:05:00Z")
        self.t.approve_roster_change(
            "RC-6", "sec-chief-01", "secretariat_chief", "首次批准",
            at="2026-09-20T10:06:00Z")
        with self.assertRaises(RuleViolationError):
            self.t.approve_roster_change(
                "RC-6", "sec-chief-01", "secretariat_chief", "重复批准",
                at="2026-09-20T10:07:00Z")


class RosterTimeTravelTest(ScenarioSetUp, unittest.TestCase):
    def test_roster_at_reconstructs_then_valid_members(self):
        self.t.submit_roster(
            "G1", ["P-HK-01", "P-GD-01"], ["S-HK-01"], "sec-hk-01",
            at="2026-09-20T10:00:00Z")
        self.t.request_roster_change(
            "RC-7", "G1", "P-HK-02", "P-HK-01", "emergency", "INC-1",
            "referee-01", at=TIPOFF)
        self.t.approve_roster_change(
            "RC-7", "referee-01", "referee", "受伤", at="2026-09-20T12:10:00Z")

        at_tipoff = views.roster_at(self.store, "G1", TIPOFF)
        self.assertIn("P-HK-01", [m["person_id"] for m in at_tipoff["players"]])
        self.assertNotIn("P-HK-02", [m["person_id"] for m in at_tipoff["players"]])

        after = views.roster_at(self.store, "G1", "2026-09-20T12:15:00Z")
        players = [m["person_id"] for m in after["players"]]
        self.assertIn("P-HK-02", players)
        self.assertNotIn("P-HK-01", players)
        # 溯源链条完整
        kinds = [h["kind"] for h in after["history"]]
        self.assertEqual(kinds, ["submitted", "approved"])
        approved = after["history"][-1]
        self.assertEqual(approved["approved_by"], "referee-01")
        self.assertEqual(approved["basis"]["rulebook"], "GBA-ASG-2026")

        explanation = views.change_explanation(self.store, "RC-7")
        self.assertEqual(explanation["status"], "approved")
        self.assertEqual(explanation["approved_by_role"], "referee")
        self.assertIn("受伤", explanation["basis"]["decision"])


class VerificationTest(ScenarioSetUp, unittest.TestCase):
    def _verify(self, reg_no, occurred_at, person="P-HK-01"):
        return self.t.verify_person(
            reg_no, "G1", person, "GATE-02", "pass", occurred_at,
            "marshal-gd-02")

    def test_offline_backfill_is_single_registration(self):
        self.clock.set("2026-09-20T11:50:00Z")
        first = self._verify("VR-0001", "2026-09-20T11:30:00Z")
        self.assertTrue(first.data["backfill"])
        # 设备恢复后补传同一编号同一内容 -> 回放，事件数不增加
        version_after_first = self.store.version()
        again = self._verify("VR-0001", "2026-09-20T11:30:00Z")
        self.assertEqual(again.seq, first.seq)
        self.assertEqual(self.store.version(), version_after_first)
        rows = views.verifications_for_game(self.store, "G1")
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["occurred_at"], rows[0]["recorded_at"])

    def test_backfill_with_different_content_rejected(self):
        self.clock.set("2026-09-20T11:50:00Z")
        self._verify("VR-0002", "2026-09-20T11:30:00Z")
        with self.assertRaises(ConflictError):
            # 同编号但核验对象不同——首次登记必须保持
            self.t.verify_person(
                "VR-0002", "G1", "P-GD-01", "GATE-02", "pass",
                "2026-09-20T11:30:00Z", "marshal-gd-02")

    def test_backfill_beyond_limit_rejected(self):
        self.clock.set("2026-09-21T18:00:00Z")  # 超过24小时
        with self.assertRaises(RuleViolationError):
            self._verify("VR-0003", "2026-09-20T11:30:00Z")


class RefereeRecordTest(ScenarioSetUp, unittest.TestCase):
    def _sign(self, score, at=FULL_TIME, reg=None):
        return self.t.sign_referee_record(
            "G1", {"score": score, "fouls": 12, "overtime": True},
            "referee-01", at, registration_no=reg)

    def test_signed_record_is_frozen(self):
        self._sign({"HK": 101, "GD": 100})
        with self.assertRaises(RuleViolationError):
            self._sign({"HK": 100, "GD": 100})

    def test_stat_correction_is_new_version_original_kept(self):
        self._sign({"HK": 101, "GD": 100}, reg="RR-SIGN-1")
        # 加时赛后统计更正（助攻/篮板修正），比分不变
        self.t.amend_referee_record(
            "G1",
            {"score": {"HK": 101, "GD": 100}, "fouls": 12,
             "overtime": True, "assists": {"P-HK-01": 9}},
            "stat_correction", "加时助攻技术统计更正",
            "tech-commissioner-01", {"clause": "STAT-REVIEW-3.2"},
            "2026-09-20T15:30:00Z", registration_no="RR-AMEND-1")
        records = views.referee_records(self.store, "G1")
        self.assertEqual(records["effective_version"], 2)
        v1, v2 = records["versions"]
        self.assertEqual(v1["kind"], "signed")
        self.assertNotIn("assists", v1["payload"])
        self.assertEqual(v2["kind"], "stat_correction")
        self.assertEqual(v2["payload"]["assists"]["P-HK-01"], 9)
        self.assertTrue(records["immutable_original_preserved"])
        # 时间点：签字之后、修正之前只能看到 v1
        before_fix = views.referee_records(
            self.store, "G1", as_of="2026-09-20T14:00:00Z")
        self.assertEqual(before_fix["effective_version"], 1)

    def test_replay_amendment_chain(self):
        self._sign({"HK": 101, "GD": 100})
        self.t.amend_referee_record(
            "G1", {"score": {"HK": 0, "GD": 0}, "void": True},
            "score_protest", "申诉受理，原判比分暂缓生效",
            "jury-01", {"clause": "PROTEST-5.1"},
            "2026-09-20T16:00:00Z")
        self.t.amend_referee_record(
            "G1", {"score": {"HK": 102, "GD": 100}, "replayed": True},
            "replay", "重赛后比分生效",
            "jury-01", {"clause": "REPLAY-6.0", "case": "PROTEST-5.1"},
            "2026-09-21T11:00:00Z")
        records = views.referee_records(self.store, "G1")
        self.assertEqual([v["version"] for v in records["versions"]], [1, 2, 3])
        self.assertEqual(records["versions"][-1]["kind"], "replay")
        self.assertEqual(records["versions"][0]["payload"]["score"],
                         {"HK": 101, "GD": 100})

    def test_amend_without_signed_record_rejected(self):
        with self.assertRaises(RuleViolationError):
            self.t.amend_referee_record(
                "G1", {"score": {}}, "stat_correction", "x",
                "jury-01", {}, FULL_TIME)


class NoticeTest(ScenarioSetUp, unittest.TestCase):
    def test_multilingual_versions_and_delivery_trace(self):
        self.t.issue_notice(
            "N-1", "全明星训练安排", "9月19日16:00训练馆B",
            ["zh-HK", "zh-MO", "zh-CN", "en"],
            {"teams": ["T-HK", "T-GD"]}, "sec-chief-01", at=D1)
        # 送达回执：香港球员收繁中/英文，广东球员收简中
        self.t.record_delivery(
            "N-1", 1, "P-HK-01", "zh-HK", "sms", "delivered",
            "2026-09-19T09:30:00Z", "notify-gw")
        self.t.record_delivery(
            "N-1", 1, "P-HK-01", "en", "push", "read",
            "2026-09-19T09:31:00Z", "notify-gw")
        self.t.record_delivery(
            "N-1", 1, "P-GD-01", "zh-CN", "sms", "failed",
            "2026-09-19T09:30:00Z", "notify-gw", detail="号码空号")
        # 训练时间调整 -> 新版本生效，旧版与送达都保留
        self.t.issue_notice(
            "N-1", "全明星训练安排（更新）", "9月19日17:00训练馆B",
            ["zh-HK", "zh-CN", "en"], {"teams": ["T-HK", "T-GD"]},
            "sec-chief-01", at="2026-09-19T12:00:00Z")
        self.t.record_delivery(
            "N-1", 2, "P-GD-01", "zh-CN", "sms", "delivered",
            "2026-09-19T12:05:00Z", "notify-gw")

        trace = views.notice_trace(self.store, "N-1")
        self.assertEqual(len(trace["versions"]), 2)
        v1, v2 = trace["versions"]
        self.assertEqual(v1["supersedes"], None)
        self.assertEqual(v2["supersedes"], 1)
        self.assertEqual(v1["delivery_summary"],
                         {"total": 3, "delivered": 2, "read": 1,
                          "failed": 1, "pending": 0})
        self.assertEqual(v2["delivery_summary"]["delivered"], 1)
        # v1 的失败回执不被 v2 掩盖
        v1_failed = [d for d in v1["deliveries"] if d["status"] == "failed"]
        self.assertEqual(v1_failed[0]["detail"], "号码空号")

    def test_delivery_unknown_version_rejected(self):
        self.t.issue_notice(
            "N-2", "交通", "班车15:00", ["zh-CN"], {}, "sec-chief-01")
        with self.assertRaises(NotFoundError):
            self.t.record_delivery(
                "N-2", 9, "P-GD-01", "zh-CN", "sms", "delivered",
                D1, "notify-gw")


class MedicalTest(ScenarioSetUp, unittest.TestCase):
    def _case(self):
        self.t.open_medical_case(
            "MC-1", "G1", "P-HK-01", "moderate", "脚踝扭伤",
            "med-officer-01", "2026-09-20T12:35:00Z")
        self.t.add_treatment(
            "MC-1", "冰敷加压包扎", "med-officer-01",
            "2026-09-20T12:45:00Z", resources=["急救包A"])
        self.t.close_medical_case(
            "MC-1", "送院复查，无骨折", "med-officer-01",
            "2026-09-20T13:30:00Z")

    def test_case_timeline_persisted(self):
        self._case()
        view = views.medical_case_view(self.store, "MC-1")
        self.assertEqual(view["severity"], "moderate")
        self.assertEqual(len(view["treatments"]), 1)
        self.assertTrue(view["closed"])

    def test_cost_allocation_validation_and_view(self):
        self._case()
        from baysports.errors import ValidationError
        with self.assertRaises(ValidationError):
            self.t.allocate_cost(
                "MC-1", 1000, "CNY", {"GD": 80, "HK": 30},
                {"mode": "percent", "rule": "粤港澳医疗分摊备忘"},
                "finance-01")
        self.t.allocate_cost(
            "MC-1", 1000, "CNY", {"GD": 60, "HK": 30, "MO": 10},
            {"mode": "percent", "rule": "粤港澳医疗分摊备忘"},
            "finance-01", occurred_at="2026-09-21T10:00:00Z")
        view = views.medical_case_view(self.store, "MC-1")
        self.assertEqual(view["cost_allocations"][0]["shares"]["HK"], 30)

    def test_medical_resource_overbooking(self):
        self.t.register_medical_resource(
            "MR-AMB", "1号救护车", "ambulance", "GD",
            "2026-09-20T09:00:00Z", "2026-09-20T15:00:00Z", 1, "med-gd-01")
        self.t.book_medical_resource(
            "BK-1", "MR-AMB", "G1", "2026-09-20T12:30:00Z",
            "2026-09-20T13:30:00Z", "med-officer-01")
        with self.assertRaises(RuleViolationError):
            self.t.book_medical_resource(
                "BK-2", "MR-AMB", "G1", "2026-09-20T13:00:00Z",
                "2026-09-20T13:20:00Z", "med-officer-01")


class AccessControlTest(ScenarioSetUp, unittest.TestCase):
    def test_cross_region_line_level_block(self):
        hk_marshal = Viewer("venue-hk-01", "港场地员", "HK", "venue_marshal")
        # 香港场地员查广东球员 -> 行级拒绝
        person = views.build_person(self.store, "P-GD-01")
        with self.assertRaises(AuthorizationError):
            authorize(hk_marshal, "venue_check", person["region"])

    def test_field_level_minimization(self):
        person = views.build_person(self.store, "P-HK-01")
        # 场地核验目的：本地区可查，但看不到证件号/保单/病历
        hk_marshal = Viewer("venue-hk-01", "港场地员", "HK", "venue_marshal")
        authorize(hk_marshal, "venue_check", person["region"])
        view = person_view(person, "venue_check")
        self.assertNotIn("travel_document", view)
        self.assertNotIn("insurance", view)
        self.assertNotIn("contacts", view)
        self.assertTrue(any(g["type"] == "asg_participation"
                            for g in view["eligibility"]))

    def test_purpose_not_allowed_for_role(self):
        marshal = Viewer("venue-hk-01", "港场地员", "HK", "venue_marshal")
        with self.assertRaises(AuthorizationError):
            authorize(marshal, "registration", "HK")

    def test_medical_officer_is_cross_region_but_masked_fields(self):
        person = views.build_person(self.store, "P-GD-01")
        officer = Viewer("med-hk-01", "港医疗官", "HK", "medical_officer")
        authorize(officer, "medical", person["region"])  # 救治不分地域
        view = person_view(person, "medical")
        self.assertIn("medical_notes", view)
        # 医疗目的不返回通行证号码等无关材料
        self.assertNotIn("travel_document", view)

    def test_registration_purpose_masks_doc_number(self):
        person = views.build_person(self.store, "P-HK-01")
        clerk = Viewer("sec-hk-02", "港秘书处", "HK", "secretariat")
        authorize(clerk, "registration", person["region"])
        view = person_view(person, "registration")
        self.assertIn("****", view["travel_document"]["number"])
        self.assertIn("****", view["insurance"]["policy_no"])

    def test_auditor_sees_full_cross_region(self):
        person = views.build_person(self.store, "P-HK-01")
        auditor = Viewer("audit-01", "审计", "GD", "auditor")
        authorize(auditor, "audit", person["region"])
        view = person_view(person, "audit")
        # 审计目的可跨区看全量、不脱敏
        self.assertEqual(view["travel_document"]["number"], "HP-HK-011234567")
        self.assertEqual(view["insurance"]["policy_no"], "POL-P-HK-01")


class VenueSlotTest(ScenarioSetUp, unittest.TestCase):
    def test_overlapping_slot_rejected(self):
        with self.assertRaises(RuleViolationError):
            self.t.register_venue_slot(
                "SLOT-DUP", "湾区体育馆A", "GD",
                "2026-09-20T12:30:00Z", "2026-09-20T16:00:00Z",
                "冲突占用", actor="venue-gd-01")

    def test_tipoff_outside_slot_rejected(self):
        self.t.register_venue_slot(
            "SLOT-TRAIN2", "训练馆C", "GD",
            "2026-09-21T09:00:00Z", "2026-09-21T12:00:00Z",
            "其他活动", actor="venue-gd-01")
        with self.assertRaises(RuleViolationError):
            self.t.schedule_game(
                "G2", "次日活动", "SLOT-TRAIN2",
                "2026-09-20T12:00:00Z", ["T-HK"], "sec-chief-01")


class GameArchiveTest(ScenarioSetUp, unittest.TestCase):
    def test_archive_reflects_exact_point_in_time(self):
        # 开球前提交名单
        self.t.submit_roster(
            "G1", ["P-HK-01", "P-GD-01"], ["S-HK-01"], "sec-hk-01",
            at="2026-09-20T10:00:00Z")
        # 加时期间裁判签字，次日完成技术统计修正
        self.t.sign_referee_record(
            "G1", {"score": {"HK": 101, "GD": 100}}, "referee-01", FULL_TIME)
        # 医疗处置发生在开球后
        self.t.open_medical_case(
            "MC-9", "G1", "P-HK-01", "mild", "擦伤", "med-officer-01",
            "2026-09-20T12:40:00Z")
        self.t.add_treatment(
            "MC-9", "消毒包扎", "med-officer-01", "2026-09-20T12:50:00Z")
        self.t.close_medical_case(
            "MC-9", "继续比赛", "med-officer-01", "2026-09-20T13:10:00Z")
        self.t.allocate_cost(
            "MC-9", 300, "CNY", {"GD": 180, "HK": 120, "MO": 0},
            {"mode": "amount", "rule": "分摊备忘"}, "finance-01",
            occurred_at="2026-09-21T09:00:00Z")

        # 开球时刻：无医疗案例、无裁判记录
        at_tipoff = views.game_archive(self.store, "G1", as_of=TIPOFF)
        self.assertEqual(len(at_tipoff["roster"]["players"]), 2)
        self.assertEqual(at_tipoff["referee_records"]["effective_version"], None)
        self.assertEqual(at_tipoff["medical_cases"], [])

        # 处置中：案例存在、有处置、尚未关闭、尚无费用分摊
        mid = views.game_archive(self.store, "G1",
                                 as_of="2026-09-20T13:00:00Z")
        case = mid["medical_cases"][0]
        self.assertEqual(len(case["treatments"]), 1)
        self.assertFalse(case["closed"])
        self.assertEqual(case["cost_allocations"], [])
        self.assertEqual(mid["referee_records"]["effective_version"], None)

        # 赛后次日：记录已签字、费用已分摊
        after = views.game_archive(self.store, "G1",
                                   as_of="2026-09-21T10:00:00Z")
        case = after["medical_cases"][0]
        self.assertTrue(case["closed"])
        self.assertEqual(case["cost_allocations"][0]["shares"]["HK"], 120)
        self.assertEqual(after["referee_records"]["effective_version"], 1)


class PersistenceTest(unittest.TestCase):
    def test_jsonl_replay_restores_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            clock = FixedClock(D1)
            store = EventStore(clock, path=path)
            t = Tournament(store, clock)
            t.register_team("T1", "一队", "GD", "a1")
            t.register_person("P1", "T1", "球员甲", "GD", "player", actor="a1")
            del store, t
            store2 = EventStore(FixedClock(D1), path=path)
            self.assertEqual(store2.version(), 2)
            kinds = [e.etype for e in store2.all()]
            self.assertEqual(kinds, ["TeamRegistered", "PersonRegistered"])


if __name__ == "__main__":
    unittest.main()
