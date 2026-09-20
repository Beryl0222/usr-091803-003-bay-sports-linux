# 湾区赛事协同台账

服务粤港澳跨地区短赛程（如粤BA 全明星赛两天赛程）的参赛资格与资源协同。港澳通行
材料、保险、票选名单、临时替补、场馆时段、医疗保障与多语言通知分散在三地工作人员
手中，本系统以**只追加事件台账**把它们串成一条可追溯的时间线。

## 核心保障

- **带有效期的参赛资格**：球员 = 人员登记 + 港澳通行材料 + 保险，三者均带
  `valid_from / valid_until`；过期签注现场核验直接拦下，续签旧记录保留不删。
- **替补只在规则窗口内进入当场名单**：申请（附依据）→ 批准（记录规程条款与批准人）
  → 在当场比赛公布的替补窗口内生效；赛前 `any` 窗口与赛中 `medical` 窗口双向严格
  匹配，窗口关闭、未批准、越权批准一律拒绝。
- **票选名单为基线**：替补在票选名单上增删，每条名单变化都能回答“谁、依据什么、
  在何时批准”。
- **最小必要查阅**：跨地区管理员只能看本属地人员，且按角色字段级授权
  （地区岗只见身份字段、医疗官只见医疗字段、协会全量），证件号等向无关角色脱敏。
- **失联补传一次登记**：现场设备用登记号作幂等键，`occurred_at` 与 `ingested_at`
  双时间戳区分实际核验时间与补传时间，重复上传只产生一条登记。
- **原始记录不可变、修正新版本化**：裁判记录提交后不可覆盖；比分申诉、技术统计
  修正走“提议 → 裁定 → 新版本发布”，原始件与裁定依据永久并存；重赛另立场次，
  原比赛记录保留。
- **赛后从任意一场、任意时刻进入**：所有查询支持 `at=` 时点参数，赛后归档汇总
  当时有效的球员与工作人员、每版多语言通知的送达回执、医疗处置进展与费用分摊。

## 运行

```bash
python3 service.py --check           # 基础配置检查
python3 service.py --port 8000       # 启动服务，访问 /health 确认身份
python3 service.py --store ledger.json   # 事件台账落盘，重启自动重放
npm test                             # 36 个领域场景 + HTTP 契约测试
```

## 接口

- `POST /api/commands` — 统一写入入口：
  `{"type": "<命令>", "actor": "<操作人>", "payload": {...},
   "occurred_at": "<可选，离线补传发生时间>",
   "idempotency_key": "<可选，设备登记号>"}`
- `GET /api/games/<id>/roster?at=` — 当时有效的当场名单
- `GET /api/games/<id>/roster-history` — 名单变化批准链
- `GET /api/games/<id>/postmortem?at=` — 赛后一站式归档
- `GET /api/games/<id>/medical?at=` — 医疗资源与处置
- `GET /api/games/<id>/expenses?at=` — 费用分摊与各方合计
- `GET /api/games/<id>/score` — 比分、申诉与修正链
- `GET /api/games/<id>/verifications` — 核验登记（含补传标记）
- `GET /api/persons/<id>/eligibility?game_id=&at=` — 资格解释
- `GET /api/persons/<id>?viewer=<管理员>&at=` — 按职责脱敏的个人资料
- `GET /api/notices/<id>` — 每版通知送达情况
- `GET /api/records/<id>/versions` — 裁判原始记录与修正版本链

## 代码结构

- `ledger.py` — 线程安全的只追加事件台账（序列号、双时间戳、幂等键、JSON 落盘重放）
- `tournament.py` — 领域服务：资格/窗口/授权/版本化规则与时点投影查询
- `service.py` — HTTP 入口（标准库实现，无第三方依赖）
- `test_tournament.py`、`test_service_contract.py` — 场景与契约测试
