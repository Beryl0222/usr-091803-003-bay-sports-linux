# 湾区赛事协同台账（bay-sports）

服务于粤港澳队伍首次参加粤BA全明星赛的短赛程协同：两天内完成注册、训练、比赛，
把**带有效期的参赛资格、场馆时段、医疗资源、多语言通知**串成一条不可篡改的事件链。

运行 `python3 service.py --check` 自检；`python3 service.py --port 8000` 启动后
`/health` 返回服务身份；加 `--store data.jsonl` 可将事件落盘、重启回放。

## 设计要点

**只追加事件溯源（baysports/store.py）**
- 每次登记都是一条不可修改、不可删除的事件，带 `occurred_at`（现场发生时间）
  与 `recorded_at`（系统接收时间）。
- 登记编号（`registration_no`）做幂等：核验设备失联后补传，同编号同内容回放原登记、
  事件数不增加；同编号不同内容直接拒绝，首次登记不可被覆盖；补传超过 24 小时拒绝。
- 裁判签字记录一经签字即冻结；重赛、比分申诉、技术统计修正都产生**新版本**，
  原始记录与每版的批准人、依据、登记编号全部保留。

**规则即代码（baysports/rules.py, tournament.py）**
- 资格三件套均带有效期：港澳通行材料、参赛保险（须覆盖至赛后次日）、
  参赛许可；进入当场名单前按**开球时刻**逐项校验。
- 替补只能出自秘书处确认的票选名单池，并经过"申请→批准"：
  - 常规：开球前 90 分钟截止，秘书处主管/裁判可批；
  - 伤病：开球前 15 分钟截止，须附医疗证明编号，医疗官/秘书处主管可批；
  - 紧急：仅开球后至第一节结束（本规程取 20 分钟），裁判/秘书处主管可批。
- 场馆时段冲突、开球不在时段内、医疗资源超容量均直接拒绝。

**最小必要查阅（baysports/auth.py）**
- 行级：默认只能查本地区人员；医疗官、裁判、审计、财务等明确跨区角色除外。
- 列级：按查阅目的裁剪字段——场地核验看不到证件号/保单/联系方式；
  医疗救治看不到通行证号码；注册岗证件号脱敏；只有审计看全量明文。

**时间点回放（baysports/views.py）**
- `roster_at(game, as_of)`：重建任一时刻有效的当场名单及每次变化的
  申请人、批准人、角色、证明材料与规程依据。
- `game_archive(game, as_of)`：赛后从任意一场比赛进入，看到当时有效的
  名单、核验、裁判记录版本、医疗处置与费用分摊。
- `notice_trace(notice)`：每版通知各自的多语言送达回执（送达/已读/失败）。

## HTTP 接口（摘要）

写接口 `POST`，读接口 `GET`；读接口与批准类接口须带
`X-Staff-Id`、`X-Role`、`X-Region` 头。错误返回稳定错误码
（`rule_violation` 409 / `forbidden` 403 / `not_found` 404 /
`validation_error` 422 / `idempotency_conflict` 409）。

| 领域 | 接口 |
|---|---|
| 注册 | `POST /teams` `/persons` `/persons/{id}/travel-document` `/insurance` `/eligibility` |
| 票选 | `POST /ballot/nominations` `/ballot/confirm` |
| 资源 | `POST /venue-slots` `/medical-resources` `/medical-resources/bookings` |
| 比赛名单 | `POST /games` `/games/{id}/roster` `/roster-changes` `/roster-changes/{id}/approve|reject` |
| 现场核验 | `POST /verifications`；`GET /games/{id}/verifications` |
| 通知 | `POST /notices` `/notices/{id}/versions/{v}/deliveries`；`GET /notices/{id}` |
| 医疗费用 | `POST /medical-cases` 及 `.../treatments|close|costs`；`GET /medical-cases/{id}` |
| 裁判记录 | `POST /games/{id}/referee-records` `.../amend`；`GET /games/{id}/referee-records` |
| 赛后追溯 | `GET /games/{id}/archive?as_of=...`、`/roster`、`/roster-changes/{id}` |
| 审计 | `GET /events`（仅 auditor / secretariat_chief） |

## 测试

`npm test`（或 `python3 -m unittest service_contract test_scenarios test_api`）共 37 个用例：
- `service_contract`：健康检查、JSON 身份、未知路由 404 的基础契约；
- `test_scenarios`：有效期资格、三类换人窗口、票选池、离线补传幂等、
  裁判记录版本链、多语言通知送达、医疗处置与费用分摊、字段/行级权限、落盘回放；
- `test_api`：真实端口上的完整 HTTP 主链路与鉴权头。
