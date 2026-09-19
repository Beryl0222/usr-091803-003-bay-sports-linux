"""粤BA全明星赛规则常量。

规则集中在此处，赛事秘书处可在规程版本中调整；领域代码不散落魔数。
"""

# 规程版本。名单变更的"依据"即引用规程版本与条款。
RULEBOOK_VERSION = "GBA-ASG-2026"

# 参赛资格类材料默认有效期（港澳通行证/签注、保险均为带有效期的资格）。
DEFAULT_ELIGIBILITY_DAYS = 7

# 保险最短须覆盖的赛后天数（费用分摊前必须有效）。
INSURANCE_MUST_COVER_DAYS_AFTER_GAME = 1

# 名单变更窗口（相对当场比赛开球）。
ROSTER_CHANGE_WINDOW = {
    # 常规换人：开球前 90 分钟截止
    "normal": {"lead_minutes": 90, "after_tipoff": False},
    # 伤病替补：凭医疗证明，开球前 15 分钟截止
    "medical": {"lead_minutes": 15, "after_tipoff": False},
    # 紧急（球员突发伤病/不到场）：允许开球后进入当场名单，但只在第一节结束前
    "emergency": {"lead_minutes": 0, "after_tipoff": True, "deadline_quarter": 1},
}

# 允许的名单变更原因
CHANGE_REASONS = ("normal", "medical", "emergency")

# 票选名单阶段
BALLOT_STAGES = ("nominated", "confirmed")

# 通知语言
LANGUAGES = ("zh-HK", "zh-MO", "zh-CN", "en")
DEFAULT_LANGUAGE = "zh-CN"

# 现场核验补传时限（设备失联后允许补录的最长间隔）
VERIFICATION_BACKFILL_LIMIT_MINUTES = 24 * 60

# 送达渠道
DELIVERY_CHANNELS = ("sms", "email", "push", "onsite")

# 裁判记录状态机
RECORD_STATUSES = ("draft", "signed", "amended")

# 人员角色
PERSON_ROLES = ("player", "staff", "official", "medical")

# 三地代码
REGIONS = ("GD", "HK", "MO")
REGION_NAMES = {"GD": "广东", "HK": "香港", "MO": "澳门"}
