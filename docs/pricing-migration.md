# 产品定价数据迁移

监控台是迁移后的定价规则、计算和核价记录的唯一运行时来源。规则版本不可变；核价记录保留输入、结果和对应版本。

## 容器通信与配置

产品定价表页面由 FastAPI/Jinja 提供。Docker Frontend 在 `127.0.0.1:3000` 通过 Nginx 反向代理到 Backend，因此在监控台登录后打开顶部导航的“产品定价表”即可使用；页面和浏览器不会填写或保存服务间 Token。

Backend 的 `/api/pricing/*` 接口使用监控台登录会话。店小秘只访问 `/api/internal/pricing/*`，并通过现有私有 Docker 网络 `dianxiaomi-pricing-bridge` 中的 `http://monitor-pricing:8000` 连接 Backend。内部接口要求 `Authorization: Bearer <PRICING_SERVICE_TOKEN>`；未配置时返回服务不可用，错误响应和应用日志不会记录 Token。内部 API 只计算核价、纸箱预测及读取目录，不提供商品写回或发布接口。

为切换到真实服务前，在监控台与店小秘各自 `.env` 中设置相同的高熵随机 `PRICING_SERVICE_TOKEN`。不要将 Token 放入 URL、前端配置、命令行历史、截图、日志或 Git。`.env.example` 留空，不包含可用密钥。确认两个容器都加入 `dianxiaomi-pricing-bridge`，店小秘容器不能与其他工作节点共享浏览器会话。

配置变更前先备份两边 `.env` 和现有容器镜像/配置；只重建监控台 Backend 与 Frontend，不创建独立定价服务，也不清理 Docker volume。Compose 的 Postgres 数据卷必须继续使用原有项目数据。

## 部署与数据导入顺序

1. 在目标监控台 worktree 中检查 `docker compose config` 与 `docker compose config --services`。服务列表应只有现有 Postgres、Backend、Frontend 等服务，不应出现 pricing 服务。
2. 备份监控台 PostgreSQL，再构建并更新 Backend/Frontend。Backend 启动时会运行 Alembic 新增定价表；迁移只建表，不删除旧列或旧表。不要运行 `docker compose down -v`。
3. 将经过只读白名单导出的旧 JSON 保存在仓库外的受控目录。使用 `python scripts/import_pricing_data.py <外部JSON路径>` 导入；命令只输出规则与记录数量。核对版本数、记录数、每条记录引用的版本，以及草稿/已确认状态。
4. 配置并验证监控台与店小秘的内部服务 Token 及私有网络，再更新店小秘容器。先用无写入报价和纸箱预测请求验证连通；浏览器不得直接携带服务 Token。
5. 登录监控台，经 3000 端口检查产品定价表，再从店小秘的内部客户端检查相同规则返回值。只有通过后才按原计划让店小秘核价流程调用；本迁移不自动修改或发布商品。

例如可先确认当前 Compose 服务名称（不会启动或重启服务）：

```powershell
docker compose config --services
```

数据库备份应写到项目目录之外的受控位置；示例中容器名按实际项目检查后替换：

```powershell
docker exec <postgres-container> pg_dump -U <db-user> -d <db-name> -Fc -f /tmp/monitor-before-pricing.dump
docker cp <postgres-container>:/tmp/monitor-before-pricing.dump <受控备份路径>
```

不要把实际 Token 放进 Compose 命令参数；Compose 从两个项目各自的 `.env` 文件读取。

## 导入范围

导入文件仅允许包含 `pricing_rule_versions` 与 `pricing_records` 两组数据及其明确列白名单。不要复制店小秘任务库、Cookie、登录凭据、浏览器会话、截图或日志。核价导出可能包含商品名称、SKU 和成本，请将文件保存在仓库之外的受控目录，并在导入完成后按本地数据保留要求处理。

导入过程先写入规则版本，再写入关联核价记录。相同数据重复导入不会重复插入；同一 ID 对应不同内容或记录引用不存在的规则版本时，整个事务回滚并报错。命令输出只包含导入计数，不显示商品名、SKU、成本或 Token。

迁移前备份监控台 PostgreSQL。导入后核对规则版本与核价记录数量、规则外键关系，以及一条草稿和一条已确认记录的状态。店小秘原 SQLite 数据库和规则文件作为回滚档案保留，不在首次切换时删除。

## 候选报价与人工审核

候选商品的版本化接口为 `/api/v1/candidates` 和 `/api/v1/jobs`；监控服务使用 `/api/v1/internal/*` 和独立的 `COORDINATION_SERVICE_TOKEN`。候选商品、交接任务和报价分别以幂等键保存，重复事件返回原 ID。

报价写入 `/api/v1/quotes` 后始终为 `pending_review`，保留候选、SKU、全部输入、实际/期望工作簿哈希、工作表与公式快照，以及普通业务规则与 WPS 公式的并列结果。人工选择规则并批准后才有 `approved_price`；店小秘服务只能通过带协调服务令牌的 `/api/v1/internal/quotes/{quote_id}/approved` 读取批准价格。`applied` 只记录用户确认的手工应用回执，不会写入商品或调用发布操作。

桌面版工作簿以 `PRICING_WORKBOOK_PATH` 指定，只读打开；`PRICING_WORKBOOK_SHA256` 默认固定为用户选定的 2026-10-18 文件 SHA-256 `CA39959A5849291737ECD360187284D15029650F836F5EF5B963AAD08E71A108`。本机隔离 Compose 应只读挂载这份文件。哈希不匹配、公式偏移或必需成本/材质/打印时间/重量/尺寸缺失时，报价保留待审核且无可应用价格。

## 回滚

- 若 UI/API 或服务连接异常，先停止店小秘的定价调用或恢复店小秘旧镜像；不要删除或覆盖店小秘旧 SQLite 与规则文件。
- 监控台 Backend 可恢复为上一版镜像。新增的定价表与记录保留在 PostgreSQL，不影响旧代码读取原有表。
- 不要为回滚运行会删除定价表的 Alembic downgrade。若确需恢复数据库备份，应安排维护窗口并确认备份时间点会覆盖哪些后续监控台数据。
- 修复后重新执行报价、包装预测、纸箱目录及登录页面验证，再恢复店小秘服务调用。

## 规则说明

- 普货的已确认业务规则为供货成本 + 包装费 ¥1.00 + 固定运费 ¥0.40，再乘倍率 2.1；重量和尺寸不改变这套规则。
- 用户选定工作簿中的普货公式用 `L4` 选择重量运费，并通过 `G10` 将商品重量加纸箱建议重量；`D23` 直接引用 `G10`，没有单独计算体积重。此结果作为并列对照，不覆盖已确认的固定 ¥0.40 规则。
- 3D 打印 PLA/PETG 已确认规则使用材料、电费、¥1.40 固定费用及对应倍率，不计增值税。工作簿公式另加 15% VAT，并在倍率前、后各加 ¥1；两套结果都保存，需人工选规则。
- 包装预测数据同步 WPS 包装表 H 列建议重量，与定价及运费公式分离。
