"""湾区赛事协同台账（bay-sports）领域包。

模块划分：

- clock/errors/rules：基础设施与赛事规则常量
- store：只追加事件库（append-only log，append 即登记，不可篡改）
- tournament：领域服务，承载注册、资格、票选、名单窗口、核验、通知、医疗与裁判记录
- views：时间点投影（赛后从任意一场比赛回放当时状态）
- auth：跨地区管理员的最小必要字段视图
- api：HTTP 适配层
"""

__all__ = ["tournament", "views", "auth", "api", "rules", "store", "errors"]
