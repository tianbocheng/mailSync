# 邮件同步规则

## 同步原则

1. 自动列出来源邮箱的所有可选择文件夹，并排除草稿文件夹。
2. 所有来源邮件统一写入目标邮箱的 `INBOX`。
3. 每封目标邮件添加两个 Synology 标签：
   - 来源账号：例如 `SYNC:790209876@qq.com`
   - 来源文件夹：例如 `BOX:收件箱`、`BOX:已发邮件`
4. 使用 SQLite 保存来源 UID、`Message-ID`、内容哈希和目标 UID，重复运行不会重复同步。
   首次接管已有目标邮箱时，如果发现相同 `Message-ID` 的未跟踪旧副本，会以来源原件重建并替换旧副本。
5. 来源邮件更换文件夹时：
   - 先在目标 `INBOX` 写入并验证新副本；
   - 添加新的账号标签和文件夹标签；
   - 再删除旧目标副本。
6. 来源邮件完全消失时，仅在一次完整、无范围限制的扫描后判定为删除，并用同样的重建方式将文件夹标签改为 `BOX:已删除邮件`。
7. 限制范围的测试命令不会执行删除对账，避免把未扫描的邮件误判为删除。

## 命令

列出来源文件夹：

```bash
python3 mail_sync.py list-folders --source 790209876@qq.com
```

同步最新一封邮件用于测试：

```bash
python3 mail_sync.py sync \
  --source 790209876@qq.com \
  --folder INBOX \
  --limit 1 \
  --newest-first
```

完整同步当前配置中的全部来源：

```bash
python3 mail_sync.py sync
```

完整同步单个来源并执行该来源的删除对账：

```bash
python3 mail_sync.py sync --source 790209876@qq.com
```

每 60 秒自动执行一次完整增量扫描：

```bash
SYNC_INTERVAL=60 python3 mail_sync.py watch
```

`SYNC_INTERVAL` 默认是每个来源 `60` 秒，命令行参数 `--interval` 可以覆盖环境变量。多个来源会在第一个周期内均匀错峰，并保持各自独立的同步周期，不会同时请求。某个来源需要单独周期时，可在该来源配置中设置 `"sync_interval": 120`。

常驻模式默认每个来源每轮处理最多 5 封变化邮件，并优先处理最新邮件，避免首次全量补齐时某个大邮箱长期独占调度器。可通过 `SYNC_BATCH_SIZE` 或来源配置中的 `"batch_size"` 调整。积压处理完成后，脚本会自动执行完整扫描和删除对账。单轮失败会记录错误并继续后续调度；进程重启时会先恢复 SQLite 中尚未完成的远程写入操作。

## Docker Compose

启动并后台运行：

```bash
docker compose up -d --build
```

查看同步日志：

```bash
docker compose logs -f mail-sync
```

停止服务：

```bash
docker compose down
```

Compose 将 `config.json` 只读挂载到容器，不会把密码复制进镜像。同步状态保存在 `mail-sync-state` 命名卷中；容器重建或 Docker 重启不会丢失增量进度。除非明确希望从头重新建立状态，否则不要执行 `docker compose down -v`。

## 配置和状态

- `config.json`：唯一配置文件，包含邮箱凭据、同步规则和 MailPlus 标签映射。
- `config.example.json`：不含密码的配置模板；首次部署先复制为 `config.json` 并填写密码。
- `mail_sync_state.sqlite3`：增量同步状态，不应删除或多实例并发写入。
