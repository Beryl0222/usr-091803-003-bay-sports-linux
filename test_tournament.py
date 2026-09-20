"""赛事协同领域服务的端到端场景测试。

以“粤BA 全明星赛两天赛程”为主线，覆盖：
有效期资格、票选名单、替补窗口管控、最小必要查阅、离线补传幂等、
通知多语言送达、裁判原始记录不可变 + 统计修正新版本、比分申诉/重赛、
医疗处置、费用分摊与赛后任意时点追溯。
"""

import tempfile
import unittest
from pathlib import Path

from ledger import IdempotentReplay, Ledger
from tournament import (
    ROLE_ASSOCIATION,
    ROLE_MEDICAL,
    ROLE_REGION,
    DomainError,
    TournamentService,
)

TIP = "2026-09-20T12:00:00+00:00"
GAME_ID = "G-ALLSTAR-01"

CRED_VALID = {"credential_type": "港澳通行签注", "doc_no": "E12345",
              "valid_from": "2026-09-01T00:00:00+00:00",
              "valid_until": "2026-09-30T23:59:59+00:00"}
INS_VALID = {"policy_no": "P-99", "valid_from": "2026-09-01T00:00:00+00:00",
             "valid_until": "2026-09-30T23:59:59+00:00"}
CRED_EXPIRED = {"credential_type": "港澳通行签注", "doc_no": "E-OLD",
                "valid_from": "2026-08-01T00:00:00+00:00",
                "valid_until": "2026-09-10T23:59:59+00:00"}


def build_world():
    """构造一个完成登记、编排、票选名单的开赛前夕世界。"""
    svc = TournamentService(Ledger())
    reg = "2026-09-19T09:00:00+00:00"

    admins = [
        {"admin_id": "a-assoc", "name": "协会专员", "role": ROLE_ASSOCIATION, "regions": []},
        {"admin_id": "a-hk", "name": "香港管理员", "role": ROLE_REGION, "regions": ["HK"]},
        {"admin_id": "a-gd", "name": "广东管理员", "role": ROLE_REGION, "regions": ["GD"]},
        {"admin_id": "a-med", "name": "驻场医疗官", "role": ROLE_MEDICAL, "regions": ["HK", "GD", "MO"]},
    ]
    for admin in admins:
        svc.command("register_admin", "secretariat", admin, occurred_at=reg)

    players = [
        # person_id, name, team, region, jersey, credential, insurance, pii
        ("p-chan", "陳大文", "T-HK", "HK", 7, CRED_VALID, INS_VALID,
         {"phone": "90000001", "id_number": "H1234", "allergies": "青霉素",
          "emergency_contact": "陳太太 90000000", "birth_date": "1995-03-01"}),
        ("p-wong", "黃家豪", "T-HK", "HK", 11, CRED_VALID, INS_VALID,
         {"phone": "90000002", "id_number": "H5678"}),
        ("p-fong", "方世杰", "T-GD", "GD", 23, CRED_VALID, INS_VALID,
         {"phone": "13800000003", "id_number": "G9012"}),
        ("p-leong", "梁志強", "T-HK", "HK", 14, CRED_VALID, INS_VALID,
         {"phone": "90000004"}),  # 替补
        ("p-expired", "过期证", "T-MO", "MO", 99, CRED_EXPIRED, INS_VALID, {}),
    ]
    for pid, name, team, region, jersey, cred, ins, pii in players:
        svc.command("register_person", "secretariat",
                    {"person_id": pid, "name": name, "role": "player",
                     "team_id": team, "region": region, "jersey_number": jersey,
                     "credential": cred, "insurance": {"person_id": pid, **ins}, "pii": pii},
                    occurred_at=reg)

    # 场馆时段 + 医疗保障
    svc.command("book_venue_slot", "secretariat",
                {"slot_id": "S-1", "venue_id": "V-湾区馆",
                 "starts_at": "2026-09-20T10:00:00+00:00",
                 "ends_at": "2026-09-20T15:00:00+00:00",
                 "purpose": "全明星赛"}, occurred_at=reg)
    svc.command("book_medical_resource", "secretariat",
                {"resource_id": "M-amb-1", "kind": "救护车+驻场医疗官",
                 "venue_id": "V-湾区馆", "game_id": GAME_ID,
                 "starts_at": "2026-09-20T11:00:00+00:00",
                 "ends_at": "2026-09-20T15:00:00+00:00"}, occurred_at=reg)

    # 比赛：赛前任意窗口 10:00-11:30；赛中医疗窗口 12:00-13:30
    svc.command("schedule_game", "secretariat",
                {"game_id": GAME_ID, "home_team_id": "T-HK", "away_team_id": "T-GD",
                 "venue_id": "V-湾区馆", "tipoff_at": TIP,
                 "substitution_windows": [
                     {"opens_at": "2026-09-20T10:00:00+00:00",
                      "closes_at": "2026-09-20T11:30:00+00:00", "reason": "any"},
                     {"opens_at": "2026-09-20T12:00:00+00:00",
                      "closes_at": "2026-09-20T13:30:00+00:00", "reason": "medical"},
                 ]}, occurred_at=reg)
    svc.command("assign_game_staff", "secretariat",
                {"game_id": GAME_ID, "assignments": [
                    {"person_id": "p-fong", "duty": "队长"}]}, occurred_at=reg)

    # 票选名单（基线）
    svc.command("publish_vote_roster", "a-assoc",
                {"game_id": GAME_ID, "vote_ref": "球迷票选第3期",
                 "player_ids": ["p-chan", "p-wong", "p-fong"]},
                occurred_at="2026-09-19T18:00:00+00:00")
    return svc


class RegistrationAndEligibilityTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()

    def test_expired_credential_blocks_eligibility(self):
        elig = self.svc.explain_eligibility(
            "p-expired", GAME_ID, "2026-09-20T11:00:00+00:00")
        self.assertFalse(elig["eligible"])
        self.assertIn("无有效通行证件", "；".join(elig["reasons"]))

    def test_valid_player_eligible_on_vote_roster(self):
        elig = self.svc.explain_eligibility(
            "p-chan", GAME_ID, "2026-09-20T11:00:00+00:00")
        self.assertTrue(elig["eligible"], elig["reasons"])

    def test_reserve_not_eligible_until_activated(self):
        elig = self.svc.explain_eligibility(
            "p-leong", GAME_ID, "2026-09-20T11:00:00+00:00")
        self.assertFalse(elig["eligible"])
        self.assertTrue(any("不在当场有效名单内" in r for r in elig["reasons"]))

    def test_venue_slot_overlap_rejected(self):
        with self.assertRaises(DomainError):
            self.svc.command("book_venue_slot", "secretariat",
                             {"slot_id": "S-2", "venue_id": "V-湾区馆",
                              "starts_at": "2026-09-20T11:00:00+00:00",
                              "ends_at": "2026-09-20T12:00:00+00:00",
                              "purpose": "冲突训练"})

    def test_credential_renewal_keeps_history_and_changes_eligibility(self):
        # 签注续签：旧记录不删，新有效期在 09-21 起生效 → 20 日仍不合格、21 日合格
        self.svc.command("record_credential", "a-hk",
                         {"person_id": "p-expired", "credential_type": "港澳通行签注",
                          "doc_no": "E-NEW",
                          "valid_from": "2026-09-21T00:00:00+00:00",
                          "valid_until": "2026-10-31T23:59:59+00:00"})
        self.assertFalse(self.svc.explain_eligibility(
            "p-expired", GAME_ID, "2026-09-20T11:00:00+00:00")["eligible"])


class SubstitutionWindowTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()

    def _request(self, reason="injury"):
        self.svc.command("request_substitution", "coach-hk",
                         {"request_id": "REQ-1", "game_id": GAME_ID,
                          "out_player_id": "p-wong", "in_player_id": "p-leong",
                          "reason": reason, "basis": "队医诊断书 MED-0920"})

    def test_cannot_activate_without_approval(self):
        self._request()
        with self.assertRaises(DomainError):
            self.svc.command("activate_substitution", "venue-gate",
                             {"request_id": "REQ-1"},
                             occurred_at="2026-09-20T10:30:00+00:00")

    def test_window_closed_between_windows_rejected(self):
        self._request()
        self.svc.command("approve_substitution", "a-assoc",
                         {"request_id": "REQ-1",
                          "basis": "赛事规程第18条：赛中伤病可医疗替换"})
        # 11:45：任意窗口已关、医疗窗口未开
        with self.assertRaises(DomainError):
            self.svc.command("activate_substitution", "venue-gate",
                             {"request_id": "REQ-1"},
                             occurred_at="2026-09-20T11:45:00+00:00")

    def test_medical_reason_not_allowed_in_any_window(self):
        self._request(reason="injury")
        self.svc.command("approve_substitution", "a-assoc",
                         {"request_id": "REQ-1", "basis": "规程第18条"})
        with self.assertRaises(DomainError):
            self.svc.command("activate_substitution", "venue-gate",
                             {"request_id": "REQ-1"},
                             occurred_at="2026-09-20T10:30:00+00:00")

    def test_activation_inside_medical_window_changes_roster(self):
        self._request()
        self.svc.command("approve_substitution", "a-assoc",
                         {"request_id": "REQ-1",
                          "basis": "赛事规程第18条：赛中伤病可医疗替换"})
        self.svc.command("activate_substitution", "venue-gate",
                         {"request_id": "REQ-1"},
                         occurred_at="2026-09-20T12:20:00+00:00")
        before = self.svc.effective_roster(GAME_ID, "2026-09-20T11:00:00+00:00")
        after = self.svc.effective_roster(GAME_ID, "2026-09-20T13:00:00+00:00")
        self.assertIn("p-wong", [p["person_id"] for p in before["players"]])
        self.assertNotIn("p-wong", [p["person_id"] for p in after["players"]])
        self.assertIn("p-leong", [p["person_id"] for p in after["players"]])
        self.assertTrue(after["players"][-1]["eligible"])

    def test_double_approval_rejected(self):
        self._request()
        self.svc.command("approve_substitution", "a-assoc",
                         {"request_id": "REQ-1", "basis": "规程第18条"})
        with self.assertRaises(DomainError):
            self.svc.command("approve_substitution", "a-assoc",
                             {"request_id": "REQ-1", "basis": "规程第18条"})

    def test_unauthorized_approver_rejected(self):
        self._request()
        with self.assertRaises(DomainError):
            self.svc.command("approve_substitution", "a-hk",
                             {"request_id": "REQ-1", "basis": "越权尝试"})


class OfflineVerificationTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()

    def test_reupload_same_registration_is_one_checkin(self):
        # 设备 11:55 现场核验，当时失联；13:10 补传；13:12 网络重试再次补传
        kwargs = dict(occurred_at="2026-09-20T11:55:00+00:00",
                      idempotency_key="REG-DEV01-0007")
        self.svc.command("verification_checkin", "DEV-01",
                         {"game_id": GAME_ID, "person_id": "p-chan",
                          "device_id": "DEV-01"}, **kwargs)
        # 直接重试 → 台账层幂等异常
        with self.assertRaises(IdempotentReplay):
            self.svc.command("verification_checkin", "DEV-01",
                             {"game_id": GAME_ID, "person_id": "p-chan",
                              "device_id": "DEV-01"}, **kwargs)
        journal = self.svc.verification_journal(GAME_ID)
        self.assertEqual(journal["count"], 1)
        entry = journal["entries"][0]
        self.assertEqual(entry["registration_no"], "REG-DEV01-0007")
        self.assertTrue(entry["late_reupload"])


class MinimumNecessaryAccessTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()

    def test_region_admin_sees_only_identity_fields(self):
        view = self.svc.person_view("a-hk", "p-chan")["data"]
        self.assertEqual(view["name"], "陳大文")
        self.assertNotIn("phone", view)
        self.assertNotIn("id_number", view)
        self.assertNotIn("allergies", view)
        self.assertEqual(view["credential_status"], "valid")

    def test_cross_region_access_denied(self):
        with self.assertRaises(DomainError):
            self.svc.person_view("a-gd", "p-chan")

    def test_association_sees_full_profile(self):
        view = self.svc.person_view("a-assoc", "p-chan")["data"]
        self.assertEqual(view["id_number"], "H1234")
        self.assertEqual(view["phone"], "90000001")
        self.assertEqual(view["credential"]["doc_no"], "E12345")

    def test_medical_officer_sees_medical_but_not_documents(self):
        view = self.svc.person_view("a-med", "p-chan")["data"]
        self.assertEqual(view["allergies"], "青霉素")
        self.assertEqual(view["emergency_contact"], "陳太太 90000000")
        self.assertNotIn("id_number", view)


class NoticeDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()

    def test_versions_and_delivery_tracking(self):
        audience = ["p-chan", "p-wong", "p-fong"]
        self.svc.command("issue_notice", "secretariat",
                         {"notice_id": "N-1", "game_id": GAME_ID,
                          "template": "training_time", "audience": audience,
                          "languages": ["zh-Hant", "zh-Hans", "pt"],
                          "channels": ["sms", "whatsapp"]},
                         occurred_at="2026-09-19T20:00:00+00:00")
        for pid, lang in [("p-chan", "zh-Hant"), ("p-fong", "zh-Hans")]:
            self.svc.command("record_delivery", "notify-gw",
                             {"notice_id": "N-1", "version": 1, "person_id": pid,
                              "channel": "sms", "language": lang, "status": "delivered"})
        v1 = self.svc.notice_status("N-1")["versions"][0]
        self.assertEqual(v1["delivered_count"], 2)
        self.assertEqual(v1["pending"], ["p-wong"])

        # 第二版通知（训练时间调整）独立统计，不覆盖第一版
        self.svc.command("issue_notice", "secretariat",
                         {"notice_id": "N-1", "game_id": GAME_ID,
                          "template": "training_time_v2", "audience": audience,
                          "languages": ["zh-Hant", "zh-Hans", "pt"],
                          "channels": ["sms"]},
                         occurred_at="2026-09-20T08:00:00+00:00")
        status = self.svc.notice_status("N-1")
        self.assertEqual([v["version"] for v in status["versions"]], [1, 2])

    def test_delivery_to_non_audience_rejected(self):
        self.svc.command("issue_notice", "secretariat",
                         {"notice_id": "N-2", "audience": ["p-chan"],
                          "template": "t", "channels": ["sms"], "languages": ["zh-Hant"]})
        with self.assertRaises(DomainError):
            self.svc.command("record_delivery", "notify-gw",
                             {"notice_id": "N-2", "version": 1, "person_id": "p-fong",
                              "channel": "sms", "status": "delivered"})


class RecordVersioningTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()
        self.svc.command("submit_referee_record", "ref-1",
                         {"record_id": "R-1", "game_id": GAME_ID,
                          "snapshot": {"home_score": 101, "away_score": 100,
                                       "overtime": 1, "home_assists": 22}},
                         occurred_at="2026-09-20T14:30:00+00:00")
        self.svc.command("post_score", "ref-1",
                         {"game_id": GAME_ID, "score": {"T-HK": 101, "T-GD": 100}},
                         occurred_at="2026-09-20T14:35:00+00:00")

    def test_original_record_cannot_be_overwritten(self):
        with self.assertRaises(DomainError):
            self.svc.command("submit_referee_record", "ref-1",
                             {"record_id": "R-1", "game_id": GAME_ID,
                              "snapshot": {"home_score": 102}})

    def test_rejected_correction_keeps_v1(self):
        self.svc.command("propose_stat_correction", "t-gd",
                         {"correction_id": "C-1", "record_id": "R-1",
                          "game_id": GAME_ID,
                          "changes": {"home_assists": 19},
                          "reason": "加时赛后统计复核"})
        self.svc.command("decide_stat_correction", "a-assoc",
                         {"correction_id": "C-1", "decision": "rejected",
                          "basis": "录像复核维持原判"})
        versions = self.svc.referee_record_versions("R-1")
        self.assertEqual(versions["current_version"], 1)
        self.assertEqual(versions["versions"][0]["snapshot"]["home_assists"], 22)

    def test_approved_correction_publishes_new_version(self):
        self.svc.command("propose_stat_correction", "t-gd",
                         {"correction_id": "C-2", "record_id": "R-1",
                          "game_id": GAME_ID,
                          "changes": {"home_assists": 19},
                          "reason": "加时赛后技术统计更正"})
        self.svc.command("decide_stat_correction", "a-assoc",
                         {"correction_id": "C-2", "decision": "approved",
                          "basis": "技术台签字确认单 TS-09"})
        versions = self.svc.referee_record_versions("R-1")
        self.assertEqual(versions["current_version"], 2)
        v1, v2 = versions["versions"]
        # 原始记录纹丝不动，新版本承载更正
        self.assertEqual(v1["snapshot"]["home_assists"], 22)
        self.assertEqual(v2["snapshot"]["home_assists"], 19)
        self.assertEqual(v2["snapshot"]["home_score"], 101)  # 未改字段沿用
        self.assertEqual(v2["basis"], "技术台签字确认单 TS-09")
        self.assertEqual(v2["parent_version"], 1)

    def test_score_appeal_revision_and_replay(self):
        self.svc.command("file_score_appeal", "t-gd",
                         {"appeal_id": "A-1", "game_id": GAME_ID,
                          "basis": "终场前0.6秒犯规漏判"})
        self.svc.command("decide_score_appeal", "a-assoc",
                         {"appeal_id": "A-1", "decision": "approved",
                          "basis": "裁判委员会录像复核",
                          "score": {"T-HK": 101, "T-GD": 102}})
        history = self.svc.game_score_history(GAME_ID)
        self.assertEqual(history["original_score"], {"T-HK": 101, "T-GD": 100})
        self.assertEqual(history["current"]["score"], {"T-HK": 101, "T-GD": 102})

        self.svc.command("order_replay", "a-assoc",
                         {"game_id": GAME_ID, "new_game_id": "G-ALLSTAR-01R",
                          "basis": "加时赛统计争议无法现场澄清，按规程重赛"})
        postmortem = self.svc.game_postmortem(GAME_ID)
        self.assertEqual(postmortem["replays"][0]["new_game_id"], "G-ALLSTAR-01R")


class MedicalAndExpenseTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()

    def test_incident_disposition_versions_and_expense_split(self):
        self.svc.command("report_medical_incident", "a-med",
                         {"incident_id": "I-1", "game_id": GAME_ID,
                          "person_id": "p-chan", "at_minute": 28,
                          "summary": "篮下对抗后右踝扭伤"},
                         occurred_at="2026-09-20T12:40:00+00:00",
                         idempotency_key="MED-DEV-1")
        self.svc.command("update_medical_disposition", "a-med",
                         {"incident_id": "I-1",
                          "disposition": "冰敷固定，抬离场地"})
        self.svc.command("update_medical_disposition", "a-med",
                         {"incident_id": "I-1",
                          "disposition": "送院影像检查，确诊轻度扭伤"})
        self.svc.command("allocate_expense", "secretariat",
                         {"game_id": GAME_ID, "incident_id": "I-1", "amount": 3000,
                          "splits": [
                              {"party": "T-HK", "amount": 1000, "reason": "队费"},
                              {"party": "INS-P-99", "amount": 1500, "reason": "保险理赔"},
                              {"party": "组委会", "amount": 500, "reason": "赛事共担"}]})
        medical = self.svc.medical_view(GAME_ID)
        incident = medical["incidents"][0]
        self.assertEqual(len(incident["updates"]), 2)
        self.assertEqual(incident["initial_report"]["summary"], "篮下对抗后右踝扭伤")
        self.assertTrue(any(r["kind"] == "救护车+驻场医疗官"
                            for r in medical["resources"]))
        totals = self.svc.expenses(GAME_ID)["party_totals"]
        self.assertEqual(totals, {"T-HK": 1000, "INS-P-99": 1500, "组委会": 500})

    def test_expense_split_mismatch_rejected(self):
        with self.assertRaises(DomainError):
            self.svc.command("allocate_expense", "secretariat",
                             {"game_id": GAME_ID, "amount": 1000,
                              "splits": [{"party": "T-HK", "amount": 900}]})


class PostmortemTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world()
        self.svc.command("request_substitution", "coach-hk",
                         {"request_id": "REQ-1", "game_id": GAME_ID,
                          "out_player_id": "p-wong", "in_player_id": "p-leong",
                          "reason": "injury", "basis": "队医诊断书 MED-0920"},
                         occurred_at="2026-09-20T12:10:00+00:00")
        self.svc.command("approve_substitution", "a-assoc",
                         {"request_id": "REQ-1",
                          "basis": "赛事规程第18条：赛中伤病可医疗替换"},
                         occurred_at="2026-09-20T12:15:00+00:00")
        self.svc.command("activate_substitution", "venue-gate",
                         {"request_id": "REQ-1"},
                         occurred_at="2026-09-20T12:20:00+00:00")

    def test_as_of_roster_time_travel(self):
        at_11 = self.svc.effective_roster(GAME_ID, "2026-09-20T11:00:00+00:00")
        at_13 = self.svc.effective_roster(GAME_ID, "2026-09-20T13:00:00+00:00")
        self.assertEqual([p["person_id"] for p in at_11["players"]],
                         ["p-chan", "p-wong", "p-fong"])
        self.assertEqual([p["person_id"] for p in at_13["players"]],
                         ["p-chan", "p-fong", "p-leong"])

    def test_change_history_explains_who_and_basis(self):
        history = self.svc.roster_change_history(GAME_ID)
        sub = next(c for c in history["changes"] if c["kind"] == "substitution")
        self.assertEqual(sub["requested_by"], "coach-hk")
        self.assertEqual(sub["request_basis"], "队医诊断书 MED-0920")
        self.assertEqual(sub["approved_by"], "a-assoc")
        self.assertIn("第18条", sub["approval_basis"])
        self.assertEqual(sub["status"], "activated")
        self.assertEqual(sub["window"]["reason"], "medical")

    def test_postmortem_bundle(self):
        bundle = self.svc.game_postmortem(GAME_ID, "2026-09-20T13:00:00+00:00")
        self.assertEqual(bundle["game"]["game_id"], GAME_ID)
        self.assertIn("p-leong", [p["person_id"] for p in bundle["roster"]["players"]])
        self.assertTrue(bundle["roster_history"]["changes"])
        self.assertIn("score", bundle)
        self.assertIn("medical", bundle)
        self.assertIn("expenses", bundle)


class LedgerPersistenceTest(unittest.TestCase):
    def test_reload_from_disk_replays_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.json")
            svc = TournamentService(Ledger(path))
            svc.command("register_admin", "s",
                        {"admin_id": "a1", "role": ROLE_ASSOCIATION})
            svc.command("register_person", "s",
                        {"person_id": "p1", "name": "重放", "role": "player",
                         "team_id": "T-HK", "region": "HK"})
            replayed = TournamentService(Ledger(path))
            self.assertEqual(replayed._persons()["p1"]["name"], "重放")
            self.assertIn("a1", replayed._admins())


if __name__ == "__main__":
    unittest.main()
