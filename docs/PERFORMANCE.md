# 性能设计

## 调度原则

服务启动时同时检查：

- CPU 核心数与内存
- `lspci` / `nvidia-smi` 识别到的 GPU
- `/dev/dri`、`/dev/kfd`、`/dev/nvidia*` 设备节点
- ONNX Runtime 实际可用 Provider
- FFmpeg 实际编译进的 hwaccel、encoder 和 decoder

执行计划基于“设备存在并且运行时可用”，避免仅根据 CPU 型号宣布硬件加速。部署覆盖文件可以显式指定推理后端，页面会显示最终执行计划。

## 各硬件路径

| 硬件 | 大模型/视觉 | Embedding | 语音转写 | 视频 |
|---|---|---|---|---|
| 纯 CPU | Ollama CPU | Ollama CPU | faster-whisper CPU | FFmpeg CPU |
| Intel 核显兼容模式 | Ollama Vulkan | Ollama Vulkan | faster-whisper CPU | QSV，失败回退 VA-API/CPU |
| Intel 核显性能模式 | OpenVINO GPU | OpenVINO GPU | 当前为 CPU，可接 OVMS Audio | QSV |
| AMD ROCm | Ollama ROCm | Ollama ROCm | CPU | VA-API |
| AMD Vulkan | Ollama Vulkan | Ollama Vulkan | CPU | VA-API |
| NVIDIA | Ollama CUDA | Ollama CUDA | faster-whisper CUDA | NVDEC/CUDA |

NAS 精简栈可使用 `compose.nas-nvidia.yml` 叠加层：CUDA 版 llama.cpp 的视觉与向量服务通过 NVIDIA Runtime 使用独显，faster-whisper 使用 CUDA float16，应用容器通过 NVDEC/CUDA 处理视频。

AMD 核显型号差异较大。ROCm 不支持时使用 Vulkan，而不是强行设置 `HSA_OVERRIDE_GFX_VERSION`；错误映射可能导致推理结果异常或进程崩溃。

## 并发预算

- 文件解析 worker 受 CPU 和内存共同约束，上限 8。
- 媒体硬件解码默认 1–3 路，避免核显共享内存被占满。
- 集成 GPU 和小显存 GPU 默认只运行 1 个推理请求。
- NAS 精简版把普通图片描述限制为 512 个视觉 token；与 1024 token 相比更适合大图库吞吐，精细文字和定位任务可通过 `NAS_AI_VISION_IMAGE_MAX_TOKENS=1024` 恢复高精度模式。
- 16 GB 以上独立显存可提升到 2 个推理请求。
- 指向同一模型服务的请求使用带交互优先级的推理门控，搜索与问答会先于后续后台索引进入队列；指向独立服务的视觉、向量和语音任务使用各自并发门控，避免无谓互相阻塞。
- 人物识别使用独立低优先级后台任务和 OpenCV DNN CPU 路径，不与 VLM/Embedding 抢占核显或独显显存；默认按逻辑 CPU 数启用最多 4 路图片并行，可用 `NAS_AI_FACE_WORKERS` 限制或提高。其余模型服务仍按 Intel/AMD/NVIDIA 路径使用加速器。
- `OLLAMA_MAX_LOADED_MODELS=2` 让默认的 2B VLM 与 0.6B Embedding 常驻；内存不足可改为 1，代价是模型切换变慢。
- `NAS_AI_INDEX_WORKERS` 独立控制单个索引批次的解析线程，不再等同于任务队列 worker 数；同一时间只保留一个全库待处理索引任务。
- 每个文件开始处理前检查 `MemAvailable` 与剩余 Swap。内存低时自动降为单路，触及紧急水位或 `NAS_AI_MIN_FREE_SWAP_MB` 时保持可取消并等待资源恢复。
- NAS 精简栈把视觉/向量服务的 batch 与 ubatch 调低到真实请求所需范围，Embedding 上下文缩至 2048，并用 Q8 KV cache 降低 CPU 内存页；Whisper 默认空闲 10 分钟后卸载。
- Web 端缩略图只有进入视口附近才请求，最多同时加载 5 张，并通过 160 项对象 URL LRU 限制浏览器内存；人物和事件列表按 60 项分页。

## 索引策略

1. `os.scandir` 迭代目录，跳过 NAS 回收站、快照和 AppleDouble 文件。
2. 以路径、大小和 `mtime_ns` 判断变化，未变化文件零内容读取。
3. 变化文件按类型解析，文本最多读取配置的上限。
4. 文本按约 1200 字符切块并保留重叠；PDF/Office 保留页码或工作表，Whisper 分段保留媒体时间戳。扫描 PDF 最多 OCR `NAS_AI_PDF_OCR_PAGES` 页。
5. Embedding 以 32 条为一批提交，降低模型调用开销。
6. 全文结果与向量结果使用 Reciprocal Rank Fusion 合并，不依赖单一召回方式；快速结果先返回，首屏最多 12 个候选再交给本地模型精准重排。
7. 快速扫描只更新文件发现状态；深度索引默认按 200 个文件分批，可选择均衡、最新、最早或小文件优先。
8. 夜间自动索引仅在配置的时间窗口、队列空闲、内存和 Swap 均满足保护线时提交下一批，服务重启后策略仍保留。
9. 模型失败的文件进入 `partial`，自动修复优先于新文件深度索引，并只重跑缺失/失败阶段。
10. 自动修复采用可配置指数退避；累计失败达到上限后转为人工检查，损坏文件不会让每分钟编排器永久占用模型。
11. 任务持续写入工作量和心跳，最近 20 个成功批次生成吞吐和 ETA；前端首页 10 秒刷新，任务页 3 秒刷新并在页面不可见时暂停轮询。

## 人物索引策略

1. 只读取尚未处理、已修改或上次解码失败的图片。
2. 检测前将长边限制为 1600 像素，减少 NAS CPU 和内存压力。
3. 以有界任务窗口并行处理图片，避免一次性创建上万任务；每个 worker 使用独立 YuNet/SFace 实例。
4. YuNet 检测人脸，SFace 生成归一化特征；特征以 float32 BLOB 保存。
5. 已命名人物使用稳定质心接收新增照片，其余人脸以保守阈值增量聚类。
6. 人脸缩略图按需生成并缓存，原图不被修改。

## 后续基准项

正式发布前应在至少五类设备上记录：每万文件扫描耗时、每千张图片描述耗时、每小时视频转写耗时、峰值内存、峰值显存、待机内存和搜索 P95。硬件调度参数应由基准数据校准，而不是无限提高并发。
