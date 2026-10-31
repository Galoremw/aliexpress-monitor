# NAS 监控台运维入口

状态：2026-10-25 已验证只读连接；未执行部署或重启。

## 访问方式

- SSH 别名：`monitor-nas`，目标 `192.168.66.115`，用户 `monitor-deploy`。
- Windows Docker CLI 已建立名为 `monitor-nas` 的 SSH context。通过 `docker --context monitor-nas ...` 显式操作 NAS；默认 Docker context 保持不变。
- 私钥保存在本机 `%USERPROFILE%\.ssh\monitor-deploy_ed25519`，由本机 ssh-agent 管理，不复制到 NAS 或仓库。
- 账号不是 NAS Web 管理员，但属于 NAS 的 `docker` 组。Docker 组能控制 NAS 上所有容器，且可能借容器挂载访问主机文件，效果近似 root；账号名称和操作约定不构成项目级权限隔离。
- 已验证 SSH 公钥登录和远程 Docker context。只在可信局域网或私有 VPN 使用。NAS 路由器端口转发和防火墙规则未核验，因此不能据此断言 SSH 未暴露公网。

## 只读检查

从监控台仓库根目录运行：

```powershell
docker --context monitor-nas ps --filter name=aliexpress-monitor --format '{{.Names}} {{.Status}}'
docker --context monitor-nas compose -f compose.yaml ps
docker --context monitor-nas compose -f compose.yaml config --services
ssh monitor-nas "curl -fsS http://127.0.0.1:8000/health"
```

2026-10-25 验证结果：NAS 上 backend、frontend、PostgreSQL 三个容器均为 healthy；Compose context 能匹配现有 `aliexpress-monitor` 项目；backend `/health` 返回 `{"status":"ok"}`。

当前队列只读状态：run `35`，日期 `2026-10-25`，`CATCH_UP / PENDING / STORE_DISCOVERY`，16 项待处理，派发关闭。此账号设置过程中未启动、暂停、恢复或重排队列。

需要重新查询队列时，可执行以下只读 SQL；不要打印 `.env` 或容器完整环境：

```powershell
@'
SELECT id,target_date,trigger,status,phase,total_count,succeeded_count,partial_count,failed_count,dispatch_enabled
FROM browser_collection_runs ORDER BY id DESC LIMIT 1;
SELECT status,count(*)
FROM browser_collection_items
WHERE run_id=(SELECT id FROM browser_collection_runs ORDER BY id DESC LIMIT 1)
GROUP BY status ORDER BY status;
'@ | ssh -o BatchMode=yes monitor-nas "docker exec -i -u postgres aliexpress-monitor-postgres-1 psql -U monitor -d monitor -At -F '|'"
```

## 部署边界

- Docker Compose 文件和构建上下文从本机当前工作区读取，Docker context 将容器操作发往 NAS；不需要授予 `monitor-deploy` 读取 NAS 上其他项目目录的权限。
- NAS 源码目录 `/vol1/docker-projects/aliexpress-monitor` 不在该账号获准的 `Codex专用` 团队共享 `/vol1/team/Codex专用` 下。尝试添加项目定向 ACL 未能赋予有效读取权限，已撤销本次添加的 ACL。不要为此授予 `/vol1/docker-projects` 或其他共享的宽泛权限。
- 当前仓库可见的项目操作脚本是 Windows `.bat` / `.ps1`；不可直接在 NAS Linux 上执行。使用仓库 `compose.yaml` 和 Docker context，不要在 NAS 猜测脚本路径。
- backend Dockerfile 的启动命令先运行 `alembic upgrade head` 再启动 Uvicorn。因此 backend 重建/重启可能执行数据库迁移。即使是 backend-only 操作，也必须先只读检查队列，并就本次具体部署取得用户确认。
- 经单独授权后，预期的 backend-only 部署命令为：

```powershell
docker --context monitor-nas compose -f compose.yaml up -d --no-deps --build backend
```

- 不得把此命令替换为全栈 `up` / `down`、数据库恢复、容器/卷删除或 `docker system prune`。这些操作每次都需要明确授权；数据库迁移也需要明确说明并确认。
- 本次仅做了只读验证，没有构建镜像、启动服务、运行迁移、重启容器或更改队列。
