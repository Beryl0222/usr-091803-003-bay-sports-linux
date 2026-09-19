"""跨地区管理员权限：职责所需，最小查阅。

两个维度：
1. 行级：默认只能看本地区（GD/HK/MO）人员，仅审计/总裁判等角色可跨区；
2. 列级：按"查阅目的"只返回必要字段，例如场地核验看不到证件号码与保单号，
   医疗人员看不到通行证号码，秘书处注册岗才能看全部材料。
"""

from dataclasses import dataclass

from .errors import AuthorizationError
from .rules import REGIONS

# 查阅目的 -> 可见字段组
PURPOSE_FIELDS = {
    "registration": (
        "identity", "contact", "travel_document", "insurance",
        "eligibility", "medical_brief",
    ),
    "venue_check": ("identity", "eligibility"),
    "officiating": ("identity", "eligibility"),
    "medical": ("identity", "contact", "medical", "insurance", "eligibility"),
    "notification": ("identity", "contact"),
    "cost_settlement": ("identity", "insurance", "medical_costs"),
    "audit": (
        "identity", "contact", "travel_document", "insurance",
        "eligibility", "medical", "medical_costs",
    ),
}

# 角色 -> (允许的目的集合, 是否可跨区)
ROLE_PROFILES = {
    "secretariat": ({"registration", "notification"}, False),
    "secretariat_chief": ({"registration", "notification", "audit"}, True),
    "venue_marshal": ({"venue_check"}, False),
    "medical_officer": ({"medical"}, True),  # 赛场医疗不分地域救治
    "referee": ({"officiating"}, True),
    "auditor": ({"audit", "cost_settlement"}, True),
    "finance": ({"cost_settlement"}, True),
}


@dataclass(frozen=True)
class Viewer:
    staff_id: str
    name: str
    region: str  # GD/HK/MO
    role: str

    def purpose_allowed(self, purpose: str) -> bool:
        profile = ROLE_PROFILES.get(self.role)
        return profile is not None and purpose in profile[0]

    @property
    def cross_region(self) -> bool:
        return ROLE_PROFILES.get(self.role, (set(), False))[1]


def authorize(viewer: Viewer, purpose: str, person_region: str | None = None) -> None:
    """抛出 AuthorizationError，否则放行。"""
    if viewer.region not in REGIONS:
        raise AuthorizationError("管理员所属地区无效", region=viewer.region)
    if not viewer.purpose_allowed(purpose):
        raise AuthorizationError(
            "该角色不允许以此目的查阅个人资料",
            staff_id=viewer.staff_id, role=viewer.role, purpose=purpose,
        )
    if person_region is not None and not viewer.cross_region and person_region != viewer.region:
        raise AuthorizationError(
            "跨地区个人资料超出职责所需范围",
            staff_id=viewer.staff_id, viewer_region=viewer.region,
            person_region=person_region, purpose=purpose,
        )


_MASK = lambda value: (value[:2] + "****" + value[-2:]) if isinstance(value, str) and len(value) > 6 else "****"


def person_view(person: dict, purpose: str) -> dict:
    """按目的裁剪人员资料。person 为 views 投影出的完整人员字典。"""
    groups = PURPOSE_FIELDS[purpose]
    out: dict = {}
    if "identity" in groups:
        out.update({
            k: person.get(k)
            for k in ("person_id", "name", "region", "team_id", "role", "jersey_no")
            if k in person
        })
    if "contact" in groups:
        out["contacts"] = person.get("contacts", {})
        out["languages"] = person.get("languages", [])
        if "emergency_contact" in person:
            out["emergency_contact"] = person["emergency_contact"]
    if "travel_document" in groups:
        doc = dict(person.get("travel_document") or {})
        if purpose != "audit" and doc.get("number"):
            doc["number"] = _MASK(doc["number"])
        out["travel_document"] = doc
    if "insurance" in groups:
        ins = dict(person.get("insurance") or {})
        if purpose != "audit" and ins.get("policy_no"):
            ins["policy_no"] = _MASK(ins["policy_no"])
        out["insurance"] = ins
    if "eligibility" in groups:
        out["eligibility"] = person.get("eligibility", [])
        out["eligible_now"] = person.get("eligible_now")
    if "medical_brief" in groups:
        # 注册岗只需知道有无禁忌，不看病历细节
        notes = person.get("medical_notes")
        out["medical_notes"] = "有备案" if notes else None
    if "medical" in groups:
        out["medical_notes"] = person.get("medical_notes")
        out["emergency_contact"] = person.get("emergency_contact")
    if "medical_costs" in groups:
        out["medical_cases"] = person.get("medical_cases", [])
    return out
