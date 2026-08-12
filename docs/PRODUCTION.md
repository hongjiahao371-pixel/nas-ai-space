# 生产运行手册

## 上线基线

生产环境至少满足：

- `.env` 使用随机长 API Token，且已创建本地管理员账号
- `.env`、SQLite、备份和向量快照权限不得向组用户或其他用户开放
- 原始照片和视频目录只读挂载，上传与回收目录独立可写
- SQLite `quick_check` 为 `ok`，Qdrant、本地视觉/向量/语音端点可达
- 数据盘可用空间不少于 2 GiB 且不少于总容量的 2%
- 48 小时内存在 SQLite 在线备份
- 最新 SQLite 备份已自动通过 `quick_check` 与外键检查，并保留校验标记
- GPU 必须通过真实推理验证，不能只根据设备名称判断
- `/api/ready` 返回 HTTP 200；warning 可带说明上线，critical error 必须先修复

`/api/health` 只证明 Web 进程可响应，供容器 healthcheck 使用；`/api/ready` 才执行数据库、模型、向量、认证、账号、磁盘、备份、索引控制器和任务心跳检查。

## 推荐生产配置

```dotenv
NAS_AI_ALLOW_QUERY_TOKEN=false
NAS_AI_INDEX_RETRY_MAX_ATTEMPTS=3
NAS_AI_INDEX_RETRY_BASE_SECONDS=300
NAS_AI_TASK_RETENTION_DAYS=30
NAS_AI_TASK_RETENTION_COUNT=2000
NAS_AI_AUTOMATIC_BACKUP_ENABLED=true
NAS_AI_AUTOMATIC_BACKUP_INTERVAL_HOURS=24
NAS_AI_AUTOMATIC_BACKUP_RETENTION=7
```

应用端口默认适合可信局域网使用。需要从公网、异地办公网或不受信任的 Wi-Fi 访问时，必须在 NAS 反向代理前启用 HTTPS，并限制来源网络；不要把 `8766` 直接映射到公网。

8 GB 内存的 NVIDIA NAS 建议保持 `NAS_AI_INDEX_WORKERS=1`，每批 50–200 个文件，并保留可用内存和 Swap 保护线。任务之间按需重启模型容器的外部编排器必须以 root 运行，但应用容器本身继续使用普通用户和只读根文件系统。

## 升级

1. 记录当前镜像、Compose 组合、容器状态和 `/api/operations/status`。
2. 调用 `POST /api/operations/backups` 创建 SQLite 在线备份。
3. 用 `scripts/create-release-backup.sh` 备份项目代码。归档会主动排除 `.env`、`data`、`uploads`、`recycle` 和 `runtime`，并以 `0600` 保存；这些持久化目录应由 NAS 快照单独保护。
4. 同步新代码并先执行 Compose `config` 校验。
5. 只重建 `app`；模型服务配置未变化时不要无谓重建或重新下载模型。
6. 启动后等待数据库迁移，检查 `/api/health`、`/api/ready`、SQLite、全部服务容器和最近错误日志。
7. 执行真实图片搜索、带来源问答、缩略图、视频 Range 206、转写和 GPU 利用率验收。

数据库迁移只做向前兼容加列。升级前在线备份和代码副本必须同时存在。

## 索引失败处理

临时错误第一次失败后进入退避等待，之后按倍数延长；达到重试上限后成为“人工检查”，外部编排器上报 degraded 但不再创建空任务。管理员应先确认：

- 原文件是否损坏、被截断或格式不受 FFmpeg/Pillow 支持
- 媒体挂载是否短暂离线
- 模型和 Qdrant 是否可达
- GPU 显存、主内存和 Swap 是否高于保护线

修复原因后，在文件详情点击手动重建会清除失败计数。不要为损坏文件反复执行全库重建。

## 备份与恢复

- SQLite 自动备份默认每 24 小时运行并保留 7 份。
- Qdrant 快照需要在运维页单独创建；恢复向量快照前应用会再备份 SQLite。
- NAS 自身的卷快照或备份套件仍需覆盖 `data`、上传、回收、模型和运行文件。
- 每季度至少做一次隔离恢复演练，验证 SQLite、Qdrant 和媒体挂载能组合恢复。

## 发布验收

```bash
python3 -m unittest discover -s tests -v
node --check app/static/app.js
bash -n scripts/index-orchestrator.sh
docker compose --env-file .env -f compose.nas-intel.yml -f compose.nas-nvidia.yml config
```

随后用 `scripts/evaluate-quality.py` 对固定真实样本执行搜索与问答指标验收。上线报告至少记录版本、容器健康、数据库完整性、语义覆盖率、终止失败数、搜索/问答 P95、GPU 实际利用率和回滚点。

依赖漏洞检查：

```bash
uvx pip-audit -r requirements.txt --progress-spinner off
```

## 回滚

1. 停止外部索引编排器，防止回滚期间继续提交任务。
2. 恢复上一版代码或镜像以及对应 Compose 配置。
3. 如果旧版本不能读取新数据库列，一般可直接忽略；若发生语义不兼容，恢复升级前 SQLite 备份和对应 Qdrant 快照。
4. 重建 `app` 并重新执行生产就绪与真实功能验收。

回滚不能只恢复 SQLite 而保留不匹配的向量集合；两者的索引版本必须成对管理。
