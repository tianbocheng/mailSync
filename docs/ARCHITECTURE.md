# Mail Sync 代码结构

后端已经收敛到 `backend/` 目录，运行时直接读取 SQLite 中的配置：

```bash
python3 backend/mail_sync.py watch
```

核心支撑代码放在 `backend/src/` 下，按职责分层：

- `backend/src/cli.py`：命令行入口和跨模块编排层。
- `backend/src/auth/`：OAuth/device-code 授权、token 刷新、凭据 profile 组装。
- `backend/src/commands/`：具体命令处理器，目前包括 `list-folders` 和 `repair-dates`。
- `backend/src/config/`：配置预检、标签映射、OAuth 配置识别。
- `backend/src/connection/`：IMAP 连接、登录、断线重连辅助。
- `backend/src/domain/`：邮件模型、日期解析、QQ Date 兼容、内容归一化和哈希缓存。
- `backend/src/imap/`：IMAP 基础协议工具、源邮箱读取和目标邮箱写入。
- `backend/src/log/`：结构化 JSON 事件输出；不命名为 `logging/`，避免和标准库撞名。
- `backend/src/monitoring/`：源邮箱准实时监听；优先 INBOX IMAP IDLE，必要时退回 STATUS 轮询。
- `backend/src/storage/`：SQLite schema 初始化/迁移、配置读取/导入、进程锁。
- `backend/src/sync/`：同步状态辅助、增量候选计算、标签集合规则、watch 调度计划和同步常量。
- `backend/src/web/`：内置 Web 管理控制台后端，包含登录、会话、API 和构建产物服务。
- `backend/src/common/`：共享错误、文件读取、时间戳等基础工具。

`backend/src/sync/` 里按同步语义继续细分：

- `engine.py`：单封邮件同步、目标副本重建和单位置清理。
- `incremental.py`：增量候选 UID、已验证位置批量刷新。
- `labels.py`：folder label 集合归一化和数据库 row 解析。
- `pending.py`：pending operation 的写入、更新、隔离和 row 转消息对象。
- `reconcile.py`：根据当前 source locations 重建目标标签集合。
- `records.py`：messages/message_locations 表的 upsert 和查询。
- `recovery.py`：pending operation 崩溃恢复。
- `rules.py`：source folder label、skip UID、Outlook Deleted Draft 判断。
- `schedule.py`：watch 模式下每个 source 的执行计划。
- `settings.py`：同步默认参数。
- `constants.py`：同步 action 常量。

前端已经收敛到 `frontend/`，使用 Vue 3、Vite 和 Element Plus：

- `frontend/src/App.vue`：登录态、hash 路由、应用壳和页面数据加载。
- `frontend/src/views/`：规则、邮箱、配置、登录四个页面。
- `frontend/src/components/`：规则抽屉、邮箱表单、协议字段、标签维护、Outlook 信息等复用组件。
- `frontend/src/api/`：后端 API 客户端，统一使用 JSON 请求体与后端交互。
- `frontend/vite.config.js`：开发环境代理 `/api`、`/login`、`/logout` 到后端，生产构建输出到 `backend/html`。

项目根目录只保留核心目录：

- `backend/config/`：旧 JSON 配置导入模板；运行时不再读取 `config.json`。
- `docs/`：同步规则、部署说明和架构文档。
- `deploy/`：Dockerfile、Compose、Docker ignore 等部署配置。
- `frontend/`：Vue 3 + Element Plus 前端源码。
- `data/`：SQLite 配置/状态库、OAuth token 等运行数据。
- `logs/`：本地运行日志；不放在 `data/`，便于和状态/token 分开管理。
- `backend/html/`：Vue 构建产物目录，由 `frontend npm run build` 生成。
- `backend/tests/`：按功能拆分的单元测试，使用 `python3 -m unittest discover -s backend/tests -t backend` 运行。

## 优化原则

- 后端启动器位于 `backend/mail_sync.py`，Docker 会复制到容器 `/app/mail_sync.py` 以保持容器入口稳定。
- 先拆纯函数和基础设施模块，再拆远程副作用较重的 IMAP 同步流程；`cli.py` 保留高层编排，避免过早制造循环依赖。
- pending operation、目标 APPEND、删除旧副本相关提交保持保守，优先保证崩溃恢复。
- 批量提交只用于可重放/幂等的状态刷新路径。
- 哈希缓存只保存哈希字符串，不缓存原始邮件内容，避免常驻进程积累大附件。

## 暂不采用

- 连接池：当前 watch 已按来源并发监控，写入目标仍需单队列语义；连接池收益有限。
- 异步 I/O：IMAP/POP 服务器兼容性和恢复语义更重要，先不引入高复杂度 async 重写。
