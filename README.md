# NAS AI Space

本地优先的 NAS 私有数据 AI 工作台。前端、后端、索引、向量库和模型服务全部运行在 NAS 上，默认不把文件发送到公网。

当前版本已经形成可运行的纵向版本：

- 图片、视频、音频、PDF、Office、EPUB 和文本文件增量扫描，并支持 PSD/PSB、AI/EPS 设计稿与 TTF/OTF/TTC 字体预览
- 中文子串/全文搜索与向量语义搜索融合排序
- Qwen3 查询指令、多条件覆盖率、同文件向量去重与独立本地小模型精准重排，不与图片识别争抢视觉队列
- 快速结果先返回、首屏后台精准重排，支持组合筛选，并能识别“昨天、上周、去年、2024年5月”等自然语言日期
- 搜索相关/不相关反馈闭环，同一查询会把人工反馈用于后续排序
- 图片 OCR/画面描述、视频关键帧理解
- 事实型图片索引描述：主体属性、空间关系、截图原文与检索同义词，并支持分批升级旧描述
- 人工描述覆盖、个人收藏与标签，修改描述后只重建该文件的本地向量
- 音视频本地 Whisper 转写，字幕块保留开始与结束时间；长视频自适应抽取最多 6 个画面并建立可跳转的时间点索引，人工校正后无损重建
- 视频搜索命中时间点与内置播放器直接跳转
- 基于本地资料的多轮问答与对话历史，来源保留 PDF 页码、Office 页/工作表和音视频时间点
- 扫描版 PDF 按页调用本地视觉模型 OCR，可用 `NAS_AI_PDF_OCR_PAGES` 限制单文件最大 OCR 页数
- 按 EXIF/媒体创建时间浏览的照片视频时间线
- Linux inotify 实时目录监听，变更合并后只触发轻量增量扫描，并保留周期扫描兜底
- 基于 GPS 元数据的离线地点相册、常用城市离线命名与基于时间/位置的自动事件相册
- 完全重复文件与视觉相似照片后台分析
- 重复副本安全回收站：必须保留至少一份同内容文件，支持原路径恢复与永久清除
- YuNet 人脸检测与 SFace 特征聚类、人物相册和人物命名
- 人物与事件支持批量合并、拆分、改封面和隐藏，人工整理结果不会被自动分析覆盖
- 搜索条件可保存为持续更新的个人智能相册
- 独立本地账号、HttpOnly Cookie 会话与 CSRF 防护、管理员/成员角色和媒体库级权限
- 多文件流式上传到独立可写空间，完成后自动建立本地索引
- SQLite 在线备份、Qdrant 集合快照/恢复、跨库索引一致性检查/修复、数据库安全压缩与审计日志
- 运维视图容器资源面板：经隔离 ops 边车（唯一挂载 docker.sock、不发布端口）查看各容器内存占用/上限与重启次数，可在线调整内存上限（docker update，256-8192 MB）并重启容器
- 快速扫描与深度 AI 索引分离，并记录元数据、视觉、语音、向量四类真实阶段状态
- `partial` 部分完成状态与自动修复队列只重跑缺失/失败阶段，可按媒体库/类型/批次安全续跑
- RAW/DNG/CR2/CR3/NEF/ARW 使用 LibRaw 解码，截断视频可降级提取首帧，尽量避免单个异常文件阻塞全库
- PSD/PSB 经 psd-tools 合成预览（超过 `NAS_AI_MAX_PSD_MB` 默认 500 MB 自动跳过），AI 复用 pdftoppm 渲染，EPS 依赖系统 Ghostscript（未安装时记为不支持），TTF/OTF/TTC 生成字体样张缩略图
- 失败索引采用指数退避并在达到重试上限后转为人工检查，避免损坏文件无限占用 GPU
- 全库语义覆盖率、批次进度、任务心跳、历史吞吐和预计剩余时间实时刷新
- 夜间自动索引、任务去重、低内存等待与交互请求优先
- CPU 负载、内存/Swap、NVIDIA 利用率/显存/温度/功耗实时遥测
- 文件详情、AI/人工描述、页码与字幕时间轴、收藏标签、单文件重建索引与失败任务重试
- Web 管理端：总览、搜索、问答、统一浏览、相册发现、任务、整理、算力、用户与运维；深色/浅色主题可切换，默认跟随系统
- 专业项目工作台：项目文件夹、自定义状态、成员角色、负责人、评级与跨项目待审阅队列
- 非破坏式素材版本栈、视频时间点/区间意见、画面手绘批注、意见解决状态与 AI 审阅摘要
- 视频审阅支持 −1/+1 帧逐帧步进（按钮或 `,` / `.` 快捷键，使用媒体实际帧率），暂停定位后评论自动记录精确时间点
- 图片、音频、视频审阅代理，自动生成封面、胶片条和波形，图片版本支持滑杆对比、视频版本支持并排对比；硬件编码失败时安全回退 CPU
- 带访问码、有效期、品牌、水印、下载和评论权限的外部审阅分享，分享令牌仅保存摘要
- 审阅意见导出 CSV 与 FCPXML 剪辑标记，支持按团队/外部范围筛选，适合继续进入剪辑和交付流程
- 审阅意见可附图片/视频附件（内外部评论均支持），附件独立存储、随机命名并随评论/项目删除自动清理
- 团队内部意见仅项目成员可见，外部分享游客只能看到标记为外部可见的意见及其附件
- 顶栏通知中心：新审阅意见、外部评论与后台任务完成实时提醒，未读角标 30 秒轮询
- `.cube` LUT 图片/视频审阅预览，视频仅生成前 30 秒轻量预览且不修改原文件
- OBJ、ASCII PLY 与 GLB/glTF 由浏览器本地 WebGL 交互预览（GLB 走 vendor 到 `app/static/vendor/three/` 的 three.js r180），不依赖 CDN 或外部 3D 服务
- 每个项目拥有独立 NAS 入库箱，可接 NAS 自带 FTP/SFTP/SMB/同步工具，并安全解包 ZIP/Eaglepack 后自动收集与索引
- 前端缩略图使用视口懒加载、并发上限和对象 URL LRU，避免一次打开数百张图片压垮低内存 NAS
- Intel、AMD、NVIDIA、纯 CPU 四类执行方案
- API Token/账号会话、只读媒体挂载、独立上传卷、路径边界检查、可取消持久化任务
- 生产就绪检查、登录失败限流、安全响应头、自动 SQLite 备份与任务历史保留策略
- 依赖漏洞基线、外部分享限流、敏感文件最小权限与备份自动完整性校验

## 运行组成

```text
浏览器
  └─ NAS AI Space（FastAPI + 原生前端 + 任务调度）
       ├─ SQLite WAL：文件、任务、全文索引、时间轴文本
       ├─ Qdrant：HNSW 语义向量索引
       ├─ Ollama / OpenVINO：Embedding、视觉理解、问答
       ├─ Speaches：Whisper 音视频转写
       ├─ OpenCV DNN：YuNet + SFace 人物识别与聚类
       └─ FFmpeg：QSV / VA-API / NVDEC / CPU 媒体处理
```

媒体目录的日常扫描和预览始终走只读挂载。精简 NAS Compose 额外提供隔离的 `/maintenance/*` 可写维护挂载，只供管理员执行经过重复副本校验的回收/恢复操作；用户上传写入独立的 `/uploads` 卷。项目入库箱位于 `NAS_UPLOAD_PATH/inbox/project-ID`，应复用 NAS 系统自带的文件服务，不需要让应用额外开放 FTP 端口。

## 部署

需要 Docker Engine 与 Docker Compose。先准备配置：

```bash
cp .env.example .env
openssl rand -hex 32
```

把生成值填入 `.env` 的 `NAS_AI_API_TOKEN`，把 `NAS_LIBRARY_PATH` 改成 NAS 上真实媒体根目录，并用 `NAS_UPLOAD_PATH`、`NAS_RECYCLE_PATH` 分别指定独立上传目录和回收目录。

首次打开页面时，系统会进入不可跳过的初始化向导。用户自行设置首位管理员的用户名、显示名称和密码，完成后直接登录；密码只以安全哈希保存在 NAS，本地账号初始化完成后不再显示注册入口。`NAS_AI_API_TOKEN` 仅用于系统集成和紧急运维，不需要交给日常网页用户。

### 纯 CPU

```bash
docker compose up -d --build
```

### Intel 核显

兼容性优先方案使用 QSV/VA-API 处理媒体、Ollama Vulkan 运行模型：

```bash
docker compose -f docker-compose.yml -f compose.intel.yml up -d --build
```

性能优先方案使用 Intel OpenVINO Model Server 2026.2.1，Embedding 和 VLM 都在核显上运行：

```bash
docker compose -f docker-compose.yml -f compose.intel-openvino.yml up -d --build
```

OpenVINO 方案默认使用约 1.2 GB 的 Qwen3 Embedding 0.6B 与 Qwen3-VL 8B INT4，建议至少 16 GB 内存。内存较小的 Intel NAS 先使用 Vulkan 方案。

已有本地 `faster-whisper` 基础镜像时，可以使用不依赖大型 Ollama 镜像的精简方案：

```bash
docker compose --env-file .env -f compose.nas-intel.yml up -d --build
```

该方案由 llama.cpp Vulkan、Qwen3-VL 2B Q8、Qwen3 Embedding 0.6B Q8、独立 Qwen3 0.6B Q8 精准重排、Qdrant 原生二进制和复用的 Whisper 服务组成。运行文件分别放在 `runtime/llama`、`runtime/qdrant` 和 `models`，模型服务只在 Compose 内网开放。`NAS_UID`、`NAS_GID`、`NAS_VIDEO_GID` 与 `NAS_RENDER_GID` 用于保持 NAS 数据权限和核显设备权限。多块 Vulkan 显卡并存时，用 `GGML_VK_VISIBLE_DEVICES` 选择模型使用的设备；媒体解码仍通过 `/dev/dri` 自动选择并在失败时回退 CPU。

内存小于 12 GB 的 NAS 可将 `NAS_AI_VISION_GGUF` 指向同模型的 `Q4_K_M` 文件，以降低常驻内存和 Swap 压力；模型别名及索引格式不变。仍有内存压力时，可将 `NAS_AI_VISION_CTX_SIZE` 调为 `4096`，并将 `NAS_AI_VISION_BATCH_SIZE` / `NAS_AI_VISION_UBATCH_SIZE` 调为 `64` / `32`；Embedding 可设 `NAS_AI_EMBEDDING_PARALLEL=1`，并将 batch/ubatch 同步调为 `32` / `32`。只有 8 GB 内存但 CPU 较强的设备，还可将 `NAS_AI_EMBEDDING_GPU_LAYERS=0`，让独显专注视觉推理并减少第二个 CUDA 上下文的主机内存占用。这些设置会略微降低单次向量生成速度，但更适合长时间索引。

### AMD 核显或独显

ROCm 支持列表内的 GPU：

```bash
docker compose -f docker-compose.yml -f compose.amd.yml up -d --build
```

不在 ROCm 支持列表内，但宿主机 Vulkan 驱动可用：

```bash
docker compose -f docker-compose.yml -f compose.amd-vulkan.yml up -d --build
```

只有确认 GPU 架构需要兼容映射时，才设置 `.env` 中的 `HSA_OVERRIDE_GFX_VERSION`。

### NVIDIA 或雷电外接 NVIDIA

宿主机需要已安装 NVIDIA 驱动与 NVIDIA Container Toolkit，并且 `docker run --gpus all ... nvidia-smi` 能看到显卡：

```bash
docker compose -f docker-compose.yml -f compose.nvidia.yml up -d --build
```

雷电外接显卡对容器而言仍是普通 NVIDIA GPU；关键是宿主机已识别设备且容器运行时完成 GPU 透传。

已有 NAS 精简栈、本地 GGUF 模型和 CUDA 版 llama.cpp 运行时时，可叠加 NVIDIA 配置，让视觉、向量、语音和视频处理使用独显：

```bash
docker compose --env-file .env -f compose.nas-intel.yml -f compose.nas-nvidia.yml up -d
```

该组合把 `NAS_LIBRARY_PATH` 与 `NAS_VIDEO_PATH` 分别只读挂载到 `/library`、`/video-library`，可独立创建照片库和视频库。启动后应同时检查页面算力计划、`nvidia-smi` 显存占用和真实视频的 CUDA 解码；仅检测到显卡型号不算加速验收通过。

启动后访问 `http://NAS-IP:8766`。首次启动会先下载约 2.5 GB 的 Ollama 模型，应用容器会等模型准备完成再开放页面；Whisper 模型在首次转写时下载。

## 为什么适合 NAS

- 扫描阶段只比较大小和纳秒级修改时间，不为未变化文件重复读取内容。
- Linux 上用 inotify 合并 5 秒内的文件变化，再触发一次快速扫描；监听不可用时默认每 15 分钟做一次兜底扫描，不会直接启动大模型索引。
- 去重先对同尺寸候选读取头尾各 1 MiB，只有头尾指纹再次一致时才读取完整文件校验，兼顾速度与准确性。
- 相似照片使用 48 位结构差异与 16 位平均色指纹，并以可取消后台任务分批写入，不占用大模型服务。
- 人物识别先把长边缩至 1600 像素，再按 CPU 规模自适应并行检测；特征向量以紧凑二进制保存，只处理新增或修改照片，命名人物在后续增量聚类中保持稳定。可用 `NAS_AI_FACE_WORKERS` 覆盖自动并发数。
- SQLite 使用 WAL、30 秒 busy timeout、批量写入与 FTS5；索引完成度以真实内容块/向量覆盖计算，不再只看任务状态。
- 默认每批处理 200 个待索引文件；8 GB NAS 建议 `NAS_AI_INDEX_WORKERS=1`，并设置可用内存与剩余 Swap 双重保护线，避免并行长任务耗尽系统余量。
- 夜间自动索引策略可在任务中心保存到 SQLite；到达时间窗口、没有其他活动任务且内存高于保护线时，调度器会自动提交下一批。
- 长时间连续索引时，可将 `scripts/index-orchestrator.sh` 以 root 所有、`0750` 权限安装到 `/usr/local/sbin/nas-ai-space-index-orchestrator`，再安装 `deploy/nas-ai-space-index.cron`。它每分钟检查任务与真实资源水位，在任务批次之间按需回收模型服务，再依次提交修复文件、待索引文件和旧版图片描述升级。临时失败按指数退避，重复失败达到上限后转为人工检查，不会无限提交空任务。
- NAS 精简栈降低 llama.cpp 的 batch/ubatch 与 Embedding 上下文长度，并使用 Q8 KV cache；Whisper 长时间空闲后自动卸载，减少 8 GB 设备上的常驻内存与 Swap。
- 向量默认存放在磁盘，Qdrant HNSW 只保留必要索引结构，降低常驻内存。
- 文本解析、媒体解码、模型推理使用独立并发上限；显存较小的设备不会盲目开多路模型任务。
- FFmpeg 先走检测到的硬件解码，驱动或格式不兼容时自动回退软件解码。
- 模型服务按任务拆分，Intel 可用 OpenVINO，NVIDIA/AMD 可用 Ollama CUDA/ROCm；不用为某一种显卡维护整套应用分支。
- 前端没有 CDN、字体或第三方分析脚本，断网仍可工作；正文字号下限 11px，颜色对比度按 WCAG AA（≥4.5:1）校准。
- 地点视图只绘制离线坐标分布，不请求公网地图；地点和事件分析均为轻量 SQLite/Python 任务。

更详细的调度逻辑见 [性能设计](docs/PERFORMANCE.md)。

## 默认模型

| 用途 | 模型 | 大小约 |
|---|---|---:|
| 中文/多语言向量 | `qwen3-embedding:0.6b` | 639 MB |
| 搜索精准重排 | `qwen3:0.6b` Q8 | 639 MB |
| 图片、视频帧、问答 | `qwen3-vl:2b` | 1.9 GB |
| 语音转写 | `Systran/faster-whisper-small` | 约 500 MB |
| 人脸检测 | OpenCV Zoo YuNet | 233 KB |
| 人脸特征 | OpenCV Zoo SFace | 36.9 MB |

内存和显存充足时，可以把视觉/问答模型改成 `qwen3-vl:4b` 或更大版本。切换 Embedding 模型导致向量维度变化时，应同时修改 `NAS_AI_QDRANT_COLLECTION`，然后重新索引。

模型服务临时不可用时，文件会进入 `partial` 状态并保留已完成的元数据或全文内容。模型恢复后优先调用 `POST /api/index/repair` 分批修复缺失阶段；失败任务会按 `NAS_AI_INDEX_RETRY_BASE_SECONDS` 指数退避，累计达到 `NAS_AI_INDEX_RETRY_MAX_ATTEMPTS` 后停止自动重试。管理员可在文件详情检查错误并手动重建，手动操作会清除终止状态。只有需要完全重建时才使用 `POST /api/reindex`。

生产部署、备份恢复、升级回滚与验收步骤见 [生产运行手册](docs/PRODUCTION.md)。固定样本的搜索和问答指标验收见 [质量验收](docs/QUALITY.md)。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
node --check app/static/app.js
bash -n scripts/index-orchestrator.sh
docker compose -f compose.nas-intel.yml -f compose.nas-nvidia.yml config
```

## 当前边界

当前版本已覆盖扫描、解析、搜索、问答、媒体发现、项目资产、版本审阅、代理媒体、LUT、基础 3D、外部分享、交付导出、多用户、入库、审计、可恢复回收站和 SQLite/Qdrant 双层备份。3D 浏览器预览当前支持 80 MB 以内的 OBJ、ASCII PLY 与 GLB（.gltf 仅支持自包含内嵌资源，Draco 压缩模型暂不支持）；大型 DCC 工程和专有格式仍应在原生制作软件中打开。应用不会自动清理文件，回收操作必须由管理员明确触发并保留至少一份完整内容相同的副本。完整灾备仍应同时使用 NAS 自身的卷快照或备份套件保护 `data`、`uploads`、`recycle` 和模型目录。
