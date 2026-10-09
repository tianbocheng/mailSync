# 邮件同步规则

## 同步原则

1. 自动列出来源邮箱的所有可选择文件夹，并排除草稿文件夹。
2. 所有来源邮件统一写入目标邮箱的 `INBOX`。
3. 每封目标邮件添加两个 Synology 标签：
   - 来源账号：例如 `SYNC:source@example.com`
   - 来源文件夹：例如 `BOX:收件箱`、`BOX:已发邮件`
4. 使用 SQLite 保存来源 UID、`Message-ID`、内容哈希和目标 UID，重复运行不会重复同步。
   首次接管已有目标邮箱时，如果发现相同 `Message-ID` 的未跟踪旧副本，会以来源原件重建并替换旧副本。
5. 来源邮件更换文件夹时：
   - 先在目标 `INBOX` 写入并验证新副本；
   - 添加新的账号标签和文件夹标签；
   - 再删除旧目标副本。
6. 来源邮件完全消失时，仅在一次完整、无范围限制的扫描后判定为删除，并用同样的重建方式将文件夹标签改为 `BOX:已删除邮件`。
7. 限制范围的测试命令不会执行删除对账，避免把未扫描的邮件误判为删除。

## 按测试邮件发现标签

Web 管理台的标签维护中可以输入一封已加标签的测试邮件主题，点击“读取邮件标签”。
系统会连接目标邮箱，在目标文件夹中按主题查找最新邮件，读取其 IMAP `FLAGS`，并自动
填充发现到的 `$synolabel_...` 内部 ID。已经保存在本地映射中的标签会同时显示名称，
发现新的 ID 后，可在下方映射表补充名称并保存。

IMAP 的标准 `FLAGS` 只包含标签 ID，不包含 MailPlus 的显示名称；因此首次发现一个新标签
时，显示名称仍需要在管理台补一次。这个流程不需要再打开 MailPlus 开发者工具或手工查询
内部 ID。

## 命令

首次从旧 JSON 配置导入 SQLite：

```bash
python3 backend/mail_sync.py import-config backend/config/config.json
```

如果数据库中已经有配置，确认要覆盖时增加 `--replace`。导入完成后，同步器
运行时只读取 SQLite，不再读取 `backend/config/config.json`。

启动 Web 管理控制台：

```bash
python3 backend/mail_sync.py serve --host 127.0.0.1 --port 24000
```

首次初始化数据库时会创建 `admin` 管理员用户。用户名可通过
`MAIL_SYNC_ADMIN_USERNAME` 覆盖。首次部署建议显式设置
`MAIL_SYNC_ADMIN_PASSWORD`；如果未设置，程序会生成一次性随机初始密码并写入
状态库同目录的 `initial-admin-password.txt`，该文件权限为 `0600`，登录并设置
永久密码后应删除。密码以 PBKDF2 哈希形式保存在 `login_users` 表，不保存明文。

列出来源文件夹：

```bash
python3 backend/mail_sync.py list-folders --source source@example.com
```

同步最新一封邮件用于测试：

```bash
python3 backend/mail_sync.py sync \
  --source source@example.com \
  --folder INBOX \
  --limit 1 \
  --newest-first
```

完整同步当前配置中的全部来源：

```bash
python3 backend/mail_sync.py sync
```

完整同步单个来源并执行该来源的删除对账：

```bash
python3 backend/mail_sync.py sync --source source@example.com
```

升级日期解析逻辑或迁移旧状态库后，执行源、状态库和目标日期校验：

```bash
python3 backend/mail_sync.py repair-dates
```

该命令使用轻量元数据扫描识别日期差异，仅重建日期不一致的目标副本；
中断后可重复运行，已验证记录不会再次处理。

部分 QQ 系统邮件没有标准 `Date` 头，MailPlus 网页端会将其显示为导入时间。
同步器会在写入目标副本时使用来源 `INTERNALDATE` 补充标准 `Date` 头，同时保留
来源原始内容哈希用于去重。升级后执行一次 `repair-dates`，命令会审计已有 QQ
和 Foxmail 记录，只重建缺少 `Date` 头的目标副本。运行历史修复前应停止常驻
同步进程并备份 `data/mail_sync_state.sqlite3`。

## Outlook OAuth 配置

Outlook.com 和 Microsoft 365 来源邮箱支持 OAuth2/XOAUTH2，不需要在配置中保存邮箱密码。

1. 在 Microsoft Entra 管理中心注册应用。若需同时支持个人 Outlook.com
   和组织账号，选择支持组织目录和个人 Microsoft 账号的应用类型。
2. 在应用的“身份验证”中启用“允许公共客户端流”。
3. 为应用添加 Office 365 Exchange Online 的委托权限
   `IMAP.AccessAsUser.All`。如果租户禁止用户自行同意，需要管理员授予同意。
4. 在 Web 管理台的邮箱账号页使用 Outlook 登录，登录成功后 OAuth token 会保存
   到 SQLite 的 `mail_accounts.auth_json` 中。如果仍在做首次迁移，可先参考
   `backend/config/config.example.json` 中的 Outlook 示例并通过 `import-config`
   导入旧配置。同时在 MailPlus 创建 `SYNC:<Outlook 邮箱>` 标签，并将
   `synology_labels` 示例中的 `$synolabel_replace_outlook` 替换为该标签真实的
   内部 ID。旧 JSON 配置的关键字段如下：

```json
{
  "email": "your-account@outlook.com",
  "imap_server": "outlook.office365.com",
  "imap_port": 993,
  "use_ssl": true,
  "auth": {
    "type": "oauth2",
    "provider": "microsoft",
    "client_id": "YOUR_MICROSOFT_ENTRA_APPLICATION_CLIENT_ID",
    "tenant": "common",
    "token_file": "data/outlook-your-account-token.json",
    "scopes": [
      "https://outlook.office.com/IMAP.AccessAsUser.All",
      "offline_access"
    ]
  }
}
```

`token_file` 只用于兼容旧 JSON 导入；新 Web 登录不再需要令牌保存目录，也不
再生成 token 文件。同步器会在访问令牌过期前自动用刷新令牌续期，并把新 token
回写到 SQLite。

首次授权执行：

```bash
docker compose run --rm mail-sync \
  authorize-outlook --source your-account@outlook.com
```

按命令输出打开 Microsoft 验证地址并输入设备码。授权成功后，可先列出文件夹
并验证同步：

```bash
docker compose run --rm mail-sync \
  list-folders --source your-account@outlook.com

docker compose run --rm mail-sync \
  sync --source your-account@outlook.com --folder INBOX --limit 1 --newest-first
```

如果令牌被管理员撤销、密码变更策略要求重新登录，重新运行
`authorize-outlook` 即可覆盖旧令牌。共享邮箱也可使用 OAuth：令牌由有权访问
共享邮箱的用户授权，但来源配置中的 `email` 应填写共享邮箱地址。

Outlook 配置中的 `"skip_deleted_drafts": true` 会忽略从草稿箱删除后进入
Deleted 文件夹的未发送邮件；正常收到或发出后再删除的邮件仍会同步。

常驻模式会为每个来源保持独立 IMAP 监视连接，默认每 3 秒并发检查所有
可同步文件夹的 `UIDNEXT`、`UIDVALIDITY` 和邮件数。发现变化后，仅查询该
文件夹中尚未记录的 UID，并由单一写入队列安全写入目标邮箱。这样多个来源
不会因串行登录而相互阻塞。

准实时模式优先使用 IMAP IDLE 监听每个来源的 `INBOX`。IDLE 连接收到服务端
变化通知后会立即触发该来源 `INBOX` 的增量同步；IDLE 连接最多保持 20 分钟，
随后主动重建连接，避免服务端空闲超时。

同时每 15 分钟执行一次完整扫描，用于对账删除、非 INBOX 文件夹变化、已有
邮件状态和监视连接无法表达的变化，避免大邮箱全量扫描频繁阻塞实时增量：

```bash
SYNC_INTERVAL=900 python3 backend/mail_sync.py watch
```

如果服务器不支持 IDLE，或者 `--folder` 指定的不是 INBOX，会退回轻量
`STATUS` 检查。`SYNC_POLL_INTERVAL` 控制这个 fallback 检查间隔，默认 `3`
秒；命令行参数 `--poll-interval` 可以覆盖。设置为 `0` 可关闭实时监视并退回
原来的定时完整扫描模式。`SYNC_INTERVAL` 控制完整对账扫描，默认每个来源 `900` 秒；
命令行参数 `--interval` 可以覆盖。多个完整扫描会均匀错峰。某个来源需要
单独完整扫描周期时，可在该来源配置中设置 `"sync_interval": 120`。

如果某个来源邮箱的 IMAP 网络需要代理，可只在该来源配置中增加
`imap_proxy`。同步器会通过 HTTP CONNECT 代理建立到 IMAP 服务器的 TCP
隧道，再在隧道内正常进行 TLS 和 IMAP 登录；未配置的邮箱仍然直连：

```json
{
  "email": "source-gmail@example.com",
  "imap_server": "imap.gmail.com",
  "imap_port": 993,
  "imap_proxy": "http://127.0.0.1:6152",
  "use_ssl": true
}
```

等价对象写法：

```json
{
  "imap_proxy": {
    "type": "http_connect",
    "host": "127.0.0.1",
    "port": 6152
  }
}
```

常驻模式默认每个来源每轮处理最多 5 封变化邮件，并优先处理最新邮件，避免首次全量补齐时某个大邮箱长期独占调度器。可通过 `SYNC_BATCH_SIZE` 或来源配置中的 `"batch_size"` 调整。积压处理完成后，脚本会自动执行完整扫描和删除对账。单轮失败会记录错误并继续后续调度；进程重启时会先恢复 SQLite 中尚未完成的远程写入操作。

## Docker Compose

启动并后台运行：

```bash
docker compose -f deploy/compose.yaml up -d --build
```

查看应用日志：

```bash
docker compose -f deploy/compose.yaml exec mail-sync tail -f /app/logs/web.log
docker compose -f deploy/compose.yaml exec mail-sync tail -f /app/logs/sync.log
```

日志目录和文件会分别收紧为 `0700`/`0600`，宿主机普通用户应通过容器读取。

Web 控制台和同步子进程会分别写入挂载日志目录：

```bash
docker compose -f deploy/compose.yaml exec mail-sync tail -f /app/logs/web.log
docker compose -f deploy/compose.yaml exec mail-sync tail -f /app/logs/sync.log
```

日志默认按 20 MiB 轮转并各保留 10 份，可通过
`MAIL_SYNC_LOG_MAX_BYTES` 和 `MAIL_SYNC_LOG_BACKUP_COUNT` 调整。

推荐使用安全重部署脚本：

```bash
./deploy/redeploy.sh
```

脚本会先使用 SQLite Backup API 在 `data/backups/` 创建一致性备份，再构建并启动
容器，等待 `/api/live` 通过；失败时恢复旧镜像。部署备份默认保留最近 10 份，
可通过 `MAIL_SYNC_DEPLOY_BACKUP_KEEP` 调整。

`/api/live` 检查 Web、SQLite 和同步主循环心跳；`/api/health` 在此基础上继续检查
规则失败和待恢复操作。同步心跳默认每 30 秒写入，10 分钟未更新才判定失活，
可通过 `MAIL_SYNC_HEARTBEAT_INTERVAL_SECONDS` 和
`MAIL_SYNC_HEARTBEAT_MAX_AGE_SECONDS` 调整。

停止服务：

```bash
docker compose -f deploy/compose.yaml down
```

构建 Docker 镜像前，需要先构建前端产物：

```bash
cd frontend
npm install
npm run build
```

开发前端时，先启动后端：

```bash
python3 backend/mail_sync.py serve --host 127.0.0.1 --port 24000
```

再启动 Vite：

```bash
cd frontend
npm run dev
```

Vite 会把 `/api`、`/login`、`/logout` 代理到本地后端。

Compose 挂载项目根目录的 `data` 和 `logs` 目录。SQLite 配置、同步状态和 OAuth token
都保存在 `data` 中；日志保存在 `logs` 中。容器重建或 Docker 重启不会丢失这些运行数据。

Linux 服务器首次部署前需要创建目录并授予容器用户写权限：

```bash
mkdir -p data logs
sudo chown -R 10001:10001 data logs
sudo chmod 700 data
sudo chmod 600 data/mail_sync_state.sqlite3 data/outlook-*.json
```

如需接续已有同步进度，应先停止本地同步程序，再使用 SQLite 的 `.backup` 命令生成一致的数据库副本，并将其放到服务器项目的 `data/mail_sync_state.sqlite3`。

## 配置和状态

- `data/mail_sync_state.sqlite3`：SQLite 配置和同步状态库，包含邮箱凭据、同步规则、
  MailPlus 标签映射、登录用户、来源 UID 和待恢复操作；不应删除或多实例并发写入。
- `backend/config/config.example.json`：旧 JSON 导入模板；运行时不再读取 `backend/config/config.json`。

目标邮箱的已读、星标等系统标志仅在创建或重建副本时从来源复制；之后允许
用户在目标邮箱独立操作，不会周期性覆盖。
