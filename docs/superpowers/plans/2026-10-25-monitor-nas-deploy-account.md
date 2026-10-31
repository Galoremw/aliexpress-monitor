# 监控台 NAS 部署账号实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `monitor-deploy` NAS SSH 账号，使其从用户本机通过密钥认证执行监控台项目运维操作，并按队列安全规则控制服务。

**Architecture:** 使用普通 NAS 用户和本机专属 Ed25519 密钥；用户加入 Docker 组，以 Docker CLI 操作 NAS 容器。密钥仅留在 Windows 用户配置中，SSH 仅走局域网或私有 VPN。部署前检查队列，运行高影响操作前单独获得用户确认。

**Tech Stack:** Windows OpenSSH Client/Agent、SSH、fnOS 用户与组管理、Docker Engine/Compose、PostgreSQL 只读队列查询。

**Spec:** `docs/superpowers/specs/2026-10-25-monitor-nas-deploy-account-design.md`

## Global Constraints

- 在 NAS 建立专用普通用户 `monitor-deploy`，不加入 NAS Web 管理员组。
- Docker 组成员可控制 NAS 上所有 Docker 容器，并可能通过特权容器或主机目录挂载访问主机文件。技术上这不是项目级隔离，而是近 root 权限；“只操作监控台”是操作约束，不是 Docker 组提供的安全边界。
- SSH 私钥只保存在用户的 Windows 电脑上，不发送到聊天或提交到仓库。
- SSH 仅通过局域网或私有 VPN 使用，不将 NAS SSH、Docker、API 或数据库端口直接暴露到公网。
- backend 部署前只读核对最新队列、派发标记和运行中项目；发现活动采集时停止部署，不自动暂停、重排或清空队列。
- backend 部署只重建/重启 backend，不触碰 frontend、PostgreSQL 或 scheduler，除非用户另行授权。
- 全栈停止、数据库迁移、管理员账号操作、数据库恢复或删除容器/数据等高影响动作，每次执行前单独确认。
- NAS 实际脚本未清点前，不照搬或执行本地 Windows `.bat`/`.ps1` 脚本。

## Review Focus

- NAS 用户 home 路径可能不是 `/home/<user>`；以 NAS 用户数据库和管理界面显示为准，不硬编码假设路径。验证：Task 3 记录 `getent passwd monitor-deploy` 的 home，并检查目录属主与权限。
- NAS 可能没有 `docker` 组或组名不同；未确认前不创建猜测的组、不授予其他组。验证：Task 2 读取现有 Docker socket 组和 Compose 访问方式。
- SSH 主机密钥变化可能是身份风险；不得自动接受新指纹。验证：Task 4 比对已记录的 NAS 主机密钥指纹，不一致则停止。
- 已加入 Docker 组但当前登录会话未刷新时，Docker 权限可能仍失败。验证：Task 4 新建 SSH 会话后检查 `id` 和只读 `docker ps`。
- 项目脚本可能停掉整栈、迁移数据库或改管理员账号。验证：Task 2 对脚本做只读分类，禁止以运行脚本作为权限测试。

---

### Task 1: Create a protected local SSH key

**Files:**
- Create: `%USERPROFILE%\.ssh\monitor-deploy_ed25519`
- Create: `%USERPROFILE%\.ssh\monitor-deploy_ed25519.pub`

**Interfaces:**
- Produces: Ed25519 public key for `monitor-deploy`; encrypted private key remains on this Windows PC.

- [x] Generate the key with Windows OpenSSH `ssh-keygen -t ed25519 -a 64 -C "monitor-deploy@192.168.66.115" -f "$env:USERPROFILE\.ssh\monitor-deploy_ed25519"`; user enters the passphrase locally.
- [x] Verify private-key ACL is limited to the current Windows user and confirm the public-key fingerprint without printing private-key content.
- [x] Check Windows `ssh-agent`; add the key to the agent. If enabling the service requires elevated Windows changes, stop and ask before changing its startup policy. (User authorized Manual startup; service is Running.)

### Task 2: Read-only NAS preflight and script inventory

**Files:**
- Read: `/vol1/docker-projects/aliexpress-monitor/compose.yaml`
- Read: NAS project script metadata and contents under `/vol1/docker-projects/aliexpress-monitor` (exclude `.env`, credentials, private keys, database files, and container secrets)

**Interfaces:**
- Consumes: Existing `Galore721` SSH access, with password and sudo input entered only in the local terminal by the user.
- Produces: Verified NAS OS/user-management method and home-directory convention, Docker group/socket ownership, Compose service names, and a classified list of NAS-runnable project scripts. The account's actual home path is read and verified after account creation in Task 3.

- [x] Connect using the existing account and inspect `id`, `getent passwd`, `getent group`, Docker socket owner/group/mode, and the project directory owner/mode; do not print environment files or full container environments.
- [x] List executable/project script files and inspect their command effects; classify read-only diagnostics, container control, data/migration, and account-management operations. NAS project source was inventoried read-only; no NAS-runnable Linux operations script was found among the listed project scripts.
- [x] Record which local Windows scripts have no NAS equivalent; do not install PowerShell or port scripts as part of this task. Windows `.bat`/`.ps1` scripts are not run on NAS.
- [x] Read the latest queue status and dispatch flag; do not mutate queue state during account setup.

### Task 3: Create the NAS account and install the public key

**Files:**
- Create on NAS: `monitor-deploy` user through the verified fnOS-supported account method.
- Create on NAS: `$DEPLOY_HOME/.ssh/authorized_keys`, where `$DEPLOY_HOME` is read from the newly created account entry.

**Interfaces:**
- Consumes: Task 1 public key and Task 2 verified home/group details.
- Produces: Non-Web-admin NAS user, membership in the existing Docker group, and a public key installed for SSH authentication.

- [x] Create `monitor-deploy` as a non-administrator account using the verified NAS account-management mechanism; do not invent a home path.
- [x] Add the account to the already verified Docker group; do not add it to NAS Web administrator or unrelated groups.
- [x] Resolve `$DEPLOY_HOME` from `getent passwd monitor-deploy`; install only the public key there with `.ssh` mode `700`, `authorized_keys` mode `600`, and correct ownership.
- [x] Do not change global SSH password-authentication policy, expose a new public port, or alter existing users' permissions.

### Task 4: Configure local SSH access and verify the privilege boundary

**Files:**
- Modify: `%USERPROFILE%\.ssh\config` (append `Host monitor-nas` with host `192.168.66.115`, user `monitor-deploy`, key path, `IdentitiesOnly yes`, and forwarding disabled)

**Interfaces:**
- Consumes: Task 1 key and Task 3 NAS account.
- Produces: Non-interactive public-key SSH alias `monitor-nas` from the current Windows login session while the key is loaded in the agent.

- [x] Verify the NAS host-key fingerprint through a trusted existing session; stop if the fingerprint differs from the previously trusted NAS key.
- [x] Append the host block without deleting or reordering existing SSH configuration: `HostName 192.168.66.115`, `User monitor-deploy`, `IdentityFile ~/.ssh/monitor-deploy_ed25519`, `IdentitiesOnly yes`, `ForwardAgent no`, `ForwardX11 no`, and `ClearAllForwardings yes`.
- [x] Test `ssh -o BatchMode=yes monitor-nas "id && docker ps --format '{{.Names}} {{.Status}}'"`; expect key authentication and read-only container listing to succeed without a password prompt.
- [x] Verify `id` shows Docker membership and no NAS Web-admin group; record that Docker still grants host-wide root-equivalent capability.
- [ ] Read-only confirm SSH is not exposed through router port forwarding or NAS firewall rules; if this cannot be verified, report the access-path uncertainty and do not claim public exposure is absent.

Verified Docker context: Windows Docker CLI context `monitor-nas` targets `ssh://monitor-nas`. `docker --context monitor-nas compose -f compose.yaml ps` and `config --services` work from the local repository. This avoids granting NAS source-directory permissions; the default local Docker context was not changed.

### Task 5: Validate project operations without destructive execution

**Files:**
- Read: NAS project Compose file and the Task 2 script inventory.
- Create: `docs/nas-monitor-deploy-operations.md` containing the script/command catalog, effects, and confirmation requirements; no credentials or secret values.

**Interfaces:**
- Consumes: Tasks 2-4 verified account, groups, project paths, and script inventory.
- Produces: User-readable catalog of all NAS-runnable monitor project operations, clearly separating read-only, backend-only, full-stack, database, and account operations.

- [x] Document each verified NAS-runnable operation with its exact command, affected services/data, and whether it requires a fresh user confirmation; local Windows scripts are explicitly excluded from NAS execution.
- [x] Verify read-only Compose `ps`, `config --services`, and backend `/health` through the SSH Docker context; do not restart services or run migrations as part of account validation.
- [x] Verify the existing queue remains unchanged and that no collection task was started, paused, resumed, or reordered.
- [x] Leave any script whose effect is unclear out of the executable catalog and report it for review.

Task 5 evidence: NAS project-path Compose fails because the deployment user cannot read its `.env`; no broader NAS share permission was granted. The local Docker context successfully reads the local Compose file and controls only the remote daemon when explicitly selected. Latest run remains `35|2026-10-25|CATCH_UP|PENDING|STORE_DISCOVERY|16|0|0|0|f` with `PENDING|16`.

## Handoff

After Task 5, report the account, verified group, SSH alias, script catalog, and any NAS operations that could not be safely enabled. Real collection acceptance remains a separate user-controlled test.
