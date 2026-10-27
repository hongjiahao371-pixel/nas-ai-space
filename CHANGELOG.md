# Changelog

All notable user-facing changes are recorded here. This project follows
Semantic Versioning.

## [1.7.2] - 2026-10-28

- 增加经恢复包和全量向量摘要校验的可选内存检索配置，保持原数据卷及模型不变，不调整备份优先级。
- 配套 Jarvis 后端区分探测超时与授权失败，保留短期已验证能力，避免一次探测失败拦截后续查询。
- 配套 Jarvis 素材任务与回答记录合并为原子提交，减少重复磁盘同步，持久化强度不变。
- 短词素材检索增加类型和修改时间复合索引，减少排序前的整类扫描，保留候选排序与权限过滤。
- 配套 Jarvis 任务轮询读取按设备隔离的有界提交快照，旧记录回退只读连接；仅返回 FULL 提交成功的结果。
- 可选只读程序页驻留保护实际运行的检索程序映射，容量、权限与身份校验不足时显示未就绪；应用保持原内存额度并禁用交换空间。
- 配套 Jarvis 短只读查询有界合并接收和结果提交，慢查询沿用同一次请求；失败回滚、取消和授权检查保持。

## [1.7.1] - 2026-10-25

- 文字查询使用独立常驻模型进程，复用相同权重；视频、音轨继续使用原多模态进程。
- 为类型、媒体库与文件编号建立筛选索引，保留原始向量、内容和权限范围。

- 备份校验流式核验向量快照，避免把恢复包解压到较小的内存临时盘；同盘打包复用不可变快照，减少一次整文件复制。数据库完整性与逐文件摘要校验仍执行。
- 交互查询以跨进程共享锁通知素材索引器让行，移除每次新查询的两次 SQLite 持久写入；进程退出自动释放，不改变资料或向量。
- 素材优先检索重叠执行独立的文字与素材向量读取；限制后台请求数，繁忙时保留串行路径，候选合并、排序和权限检查保持一致。
- 将查询向量缓存锁与模型请求锁分离，已有缓存不再等待无关模型请求；接口附带分段耗时用于诊断。

## [1.7.0] - 2026-10-23

- 修正 Intel Corporation 被 ATI 子串误判为 AMD 的硬件识别；补充下载网络设置与明确的重试提示。

- 浏览器安装与维护向导、完整素材检索自动准备、应用内点选资料目录。
- 经校验的正式版升级与程序回退，统一 Git / 压缩包安装；双索引完整恢复保留旧数据。
- 新用户从空环境安装与生命周期验收结果单独记录。

## [1.6.5] - 2026-10-11

- 可选 `media_first=true` 优先使用直接素材向量，有有效候选时跳过串行Qwen；无候选、陈旧或服务失败时仍走原文本回退。默认完整融合保留。
- 常规搜索权限使用现有原生媒体库过滤，不再把相同授权展开成全量文件编号重复查询。日期、人物、事件等附加条件保留SQL下推与原权限检查。
- 新增媒体优先、回退、空授权和API过滤的回归；速度须区分首次模型输入、客户端缓存与语音合成，不将热缓存当新问题速度。

## [1.6.4] - 2026-10-11

- 新增按媒体库授权的应用只读密钥：仅检索、问答、看图与预览，服务端默认拒绝管理及修改接口；支持撤销和到期，数据库只存摘要。
- 用户与权限页面支持创建、复制一次性密钥和撤销。前端资源 v56。
- 视频可保存已观看的人工确认时间点，同一描述优先返回该画面；明确保留原模型首位，素材版本变化后失效。
- 镜像补齐 Ghostscript，浏览器内置本地 Draco WASM 解码器，无需外部 CDN。
- 修正 Jarvis 工具注册状态的验收表述；生产接入与视频细节核对将在部署后记录实际结果。

## [1.6.3] - 2026-10-11

### Fixed
- Distinguish processable work from terminal failures; a drained queue no longer advertises an ETA for files that require manual recovery.
- Scope terminal-failure counts to visible libraries and current source revisions.
- Add an authenticated administrator-only single-file media retry, preserving other failed files.
- Audio segment end timestamps use the actual decoded PCM length rather than the requested window.

## [1.6.2] - 2026-10-10

### Improved
- Optional file-scoped refinement reuses up to six indexed candidates and reads at most two additional actual boundary frames, ranks them by media similarity and exposes playback alternatives without changing either index.
- Newly processed videos include bounded opening/closing samples. Existing indexed files remain valid; no automatic library rebuild.
- Validate PCM samples before embedding; optional audio failure preserves successful visual points and records permission-scoped partial-coverage warnings.
- Snapshot creation uses a bounded longer timeout; interrupted downloads are never published or used to expire verified backups.
- Same-generation retries remove obsolete extra media points after successful publication.
- Truncated images remain failed with actionable backup-recovery guidance; no missing bytes are fabricated.

### Limits
- Refinement is an optional sampled visual search, not verified counting, OCR or full-video action recognition. Independent audio quality and whole-library retrieval quality remain unverified.

## [1.6.1] - 2026-10-08

### Added
- File-scoped video moment search, chronological sampled-frame browsing and authenticated actual-frame previews with direct playback seeking.
- Bounded in-memory previews, permission/revision validation and stale dialog-response protection.
- Authenticated integration capability discovery and bounded, non-persistent image questions; async Jarvis backend client with source-aware search, evidence questions and candidate video moments.

### Notes
- Candidate frames support visual verification; fine counts and UI states still require review. No extra visual-model inference is added to ordinary search, and neither index needs rebuilding.

## [1.6.0] - 2026-10-08

### Added
- Direct image query uploads with normalized region cropping and permission/revision checks.
- Independent media index controls (pause/resume, processing window/order, bounded retries and recent throughput/ETA), shared interactive leases and two-service ops monitoring.
- Coordinated metadata and dual-vector recovery sets, integrity verification and restoration into a fresh isolated environment.
- Bounded scene-aware video samples and audio-track vectors, non-destructive video clip export/download.
- Native DOCX, PDF and PPTX artifact downloads and an optional durable webhook outbox.

### Fixed
- Visual retrieval scores no longer depend on generated-caption keyword coverage.
- HEIC/HEIF/AVIF caption upgrades always convert to JPEG; terminal failures can be retried explicitly.
- Coverage separates completed and inapplicable stages, vector backups expire after 48 hours, galleries collapse unused inspectors and unchanged scans can be hidden.

## [1.5.0] - 2026-10-08

### Added

- Optional local EmbeddingGemma 2 direct image, sampled video-frame and sampled
  audio-segment retrieval, stored in a separate 768-dimensional collection.
- A restartable background media indexer with source-change detection, memory
  headroom protection, retry backoff and permission-scoped coverage status.
- Media provenance and matching video/audio timestamps in search results.

### Fixed

- Continue semantic retrieval even when the first lexical search returns no
  candidates, so caption omissions cannot prevent direct-media discovery.
- Preserve direct-media matches when a text-only reranker has insufficient
  caption evidence; retain text search when the optional model is unavailable.
- Update pypdf to 6.19.0 to resolve the eight advisories reported by the release
  dependency audit, including the NAS appliance patch image.

## [1.4.1] - 2026-08-27

### Added

- Preflight checks for Docker-storage headroom, GHCR, Docker Hub, the Ollama
  registry and the exact prebuilt application manifest.
- `scripts/nas-ai restore` for verified SQLite rollback with an automatic
  pre-restore backup and post-restore Qdrant consistency repair.
- A recoverable `scripts/nas-ai uninstall` command that removes containers and
  the Compose network while preserving configuration, files, databases and
  model/vector volumes.
- A versioned release archive and `SHA256SUMS` file attached to every GitHub
  Release.

### Changed

- The quick start now distinguishes five-minute configuration from the longer
  first image/model download, and puts the x86-64 support boundary before the
  install commands.
- Setup rejects media, upload and recycle roots that are equal or nested, which
  prevents an accidental writable overlap with the source library.

### Fixed

- New-user diagnostics now surface low disk space and blocked registries before
  a long first-start attempt.
- SQLite backups no longer fail on NAS bind mounts whose ACL permits file
  creation but rejects changing the backup directory mode; each backup and its
  verification marker remain restricted to mode `0600`.

## [1.4.0] - 2026-08-27

### Added

- Apache-2.0 project license, third-party notices, security policy and
  contribution guide.
- `scripts/nas-ai` guided setup, hardware profile selection, preflight doctor,
  lifecycle commands, application backup and safe update workflow.
- Beginner quick start, hardware decision table, public-release checklist and
  continuous-integration checks.
- Tagged releases publish a prebuilt x86-64 application image; installation
  falls back to a local build if the registry image is unavailable.
- First-use checklist that guides a new administrator from media-library setup
  through scan, indexing and first search.

### Changed

- Public documentation no longer contains private repository names, production
  backup filenames or appliance-specific absolute paths.
- Release and dependency configuration is validated automatically before a
  public build is accepted.
- First-start guidance now includes container-image disk requirements and
  resumable restart instructions.

### Fixed

- `NAS_AI_BUILD_LOCAL=true` is honored when stored in `.env`, while an exported
  shell value still takes precedence.
- Mobile home-page search examples wrap instead of clipping the final example
  beyond a narrow viewport.
- Clean installations run the application with the setup user's UID and GID,
  so private host data directories remain writable without restoring container
  root capabilities.

## [1.3.2] - 2026-08-25

### Fixed

- Full reindex processes the complete requested scope and rejects conflicting
  index tasks.
- Index task deduplication is scoped by library, kind or exact file set.
- Background workers recover from transient SQLite startup and cleanup errors.
- Public project reviews paginate beyond 200 assets.
- Video review uses the real frame rate.
- Concurrent uploads cannot overwrite files with the same name.
