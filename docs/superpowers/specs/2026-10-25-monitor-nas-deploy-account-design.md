# 监控台 NAS 部署账号设计

**状态：** 用户已确认并授权实施；账号与 SSH Docker context 已建立，实际部署未执行
**日期：** 2026-10-25

## 目标

为速卖通监控台建立一个专用 NAS SSH 账号，使 Codex 可从用户本机运行监控项目的部署和运维操作，减少每次部署都让用户在 PowerShell 手动输入 NAS 密码的步骤。

## 已确认的用户要求

- 账号用于速卖通监控台项目，不作为其他 NAS 项目的日常操作账号。
- 用户选择将账号加入 Docker 组，并已确认其近似 NAS root 的权限影响。
- SSH 私钥只保存在用户的 Windows 电脑上，不发送到聊天或提交到仓库。
- SSH 仅通过局域网或私有 VPN 使用，不将 NAS SSH、Docker、API 或数据库端口直接暴露到公网。
- 重启服务前检查采集队列；正在采集时拒绝重启。全栈停止、数据库迁移和管理员账户变更等高影响操作，在执行前仍单独征求用户确认。

## 当前证据与待核验项

- 本机专用 Ed25519 密钥已生成、受 Windows 文件 ACL 保护并加载到 ssh-agent；NAS SSH 主机密钥已通过既有可信会话核对。`monitor-nas` 别名可在 BatchMode 下公钥登录。
- NAS 用户 `monitor-deploy` 已由 fnOS 创建为非 Web 管理员；home 为 `/home/monitor-deploy`，`.ssh` 权限 `700`、`authorized_keys` 权限 `600`。只加入现有 `docker` 组（GID 994），没有加入 NAS 管理员组。该组对 NAS 所有容器及潜在主机挂载有近 root 权限。
- Windows Docker CLI 已创建显式 context `monitor-nas`，目标为 `ssh://monitor-nas`。默认本地 Docker context 未改变。通过此 context 已只读验证 NAS `docker ps`、本仓库 `docker compose ps`、`config --services` 与 backend `/health`。
- fnOS 用户页面显示 `Codex专用` 团队共享路径为 `/vol1/team/Codex专用`；监控项目路径为 `/vol1/docker-projects/aliexpress-monitor`，两者不同。尝试添加仅针对该监控项目的 ACL 后，实际文件仍不可读；本次添加的 ACL 已撤销。没有扩大共享权限。
- 直接对 NAS 项目路径执行 Compose 解析仍因 `.env permission denied` 失败；不需要此权限，因为本机 Docker context 可使用本机仓库的 Compose 配置连接 NAS daemon。
- 可见项目脚本为 Windows `.bat` / `.ps1`，没有安装 PowerShell 或在 NAS 运行这些脚本。backend 容器启动命令会先执行 `alembic upgrade head`，重建 backend 仍可能触发迁移。
- 当前最新队列只读状态为 run 35，`2026-10-25 / CATCH_UP / PENDING / STORE_DISCOVERY`，16 项待处理、派发关闭；设置过程中未更改队列或任何服务。
- 未核实 NAS 路由器端口转发与防火墙规则，不能声称 SSH 未对公网开放。未执行实际部署、镜像构建、数据库迁移或容器重启。

## 方案

### 账号与认证

- 在 NAS 建立专用普通用户 `monitor-deploy`，不加入 NAS Web 管理员组。
- 在用户本机生成独立 Ed25519 SSH 密钥。私钥使用本机文件 ACL 保护并由 Windows OpenSSH Agent 管理；公钥仅安装到该 NAS 账号。
- 首次连接时核对 NAS SSH 主机密钥指纹，并为本机配置 `monitor-nas` SSH 别名。不得关闭现有全局认证策略或创建公网入口。
- NAS 账号只允许从可信局域网或私有 VPN 可达。若当前网络没有 VPN，则只在本地网络部署，不开放公网 SSH。

### Docker 权限与项目范围

- 将 `monitor-deploy` 加入 NAS Docker 组，以便它运行项目所需的容器操作。
- Docker 组成员可控制 NAS 上所有 Docker 容器，并可能通过特权容器或主机目录挂载访问主机文件。技术上这不是项目级隔离，而是近 root 权限；“只操作监控台”是操作约束，不是 Docker 组提供的安全边界。
- 不向账号授予 NAS Web 管理员权限。NAS 项目文件权限只在盘点后按执行脚本所需最小范围调整；密钥、密码、令牌不写入脚本、工作区或日志。
- 在登录密钥配置并获准后，先只读清点 NAS 项目脚本与 Compose 服务，将脚本分类为只读诊断、容器控制、数据/迁移和账号管理。Windows 专用脚本保持在 Windows 端；需要 NAS 执行的操作使用 NAS 实际存在且审核过的脚本/命令，不直接照搬本地脚本。

### 运维规则

- backend 部署前只读核对最新队列、派发标记和运行中项目；发现活动采集时停止部署，不自动暂停、重排或清空队列。
- backend 部署只重建/重启 backend，不触碰 frontend、PostgreSQL 或 scheduler，除非用户另行授权。
- 全栈停止、数据库迁移、管理员账号操作、数据库恢复或删除容器/数据等高影响动作，每次执行前单独确认。
- 保存当前镜像/源文件回滚点；部署后核对容器状态、`/health`、队列未变化，并明确区分服务健康与真实采集验收。

## 验收

- SSH 密钥认证可用，连接过程不再要求 NAS 账号密码；私钥仅位于本机且未进入仓库。
- `monitor-deploy` 不是 NAS Web 管理员，但能按预期运行 Docker 操作；同时再次向用户明确验证 Docker 组的全 NAS 容器权限。
- NAS 实际脚本清单与每个操作的作用范围已记录；不把 Windows 脚本当成可直接运行的 NAS 脚本。
- 只读检查可查看项目容器状态和队列；backend 部署验证不会修改队列，并且不重启未授权服务。
- 验证 NAS 管理端、SSH 和监控 API 未被直接暴露到公网。

## 不在范围内

- NAS Web 管理员账号、全局 root SSH 登录或向公网开放 SSH/Docker/API/数据库端口。
- 将私钥、密码或 API Token 交给聊天、写入仓库或同步到 NAS。
- 自动执行数据库迁移、全栈停止、队列重排/恢复或扩展端真实采集。
- 在 NAS 实际脚本尚未盘点前新增或替换现有脚本。
