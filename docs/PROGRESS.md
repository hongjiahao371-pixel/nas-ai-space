# NAS AI Space 部署与改造进度记录

> 更新时间：2026-08-03 23:00（北京时间）· 记录人：Kimi CLI 会话

## 一、部署现状

- **访问地址**：http://192.168.5.29:8766 （应用版本 1.1.0，健康检查 ok:true）
- **NAS**：绿联 DXP4800 Pro（i3-1315U / 15GB 内存 / Intel 核显 Vulkan）
- **部署目录**：`/volume1/docker/nas-ai-space`（compose：`compose.nas-intel.yml`，媒体库 `/volume1/photo`）
- **运行栈**（6 容器，全部 healthy）：
  - app（主应用，限 1.5GB）
  - vision（Qwen3-VL-2B **Q4_K_M** + mmproj Q8，**限 5GB**——OOM 调过两轮：3G→4G→5G）
  - embedding（Qwen3-Embedding-0.6B Q8 纯 CPU，限 1GB）
  - qdrant（限 768MB）· speech（faster-whisper-tiny，限 768MB）
  - ops（资源代理边车，唯一挂 docker socket，限 128MB）
- **GitHub**：hongjiahao371-pixel/nas-ai-space（私有），最新提交 `a21a670`，三方（GitHub/NAS/本地）完全同步
- **本地工作区**：`/tmp/nas-ai-deploy/nas-ai-space`（git 工作区，干净无未提交改动）

## 二、完成的大的里程碑

1. **部署**：旧版原地升级到 v1.0.6（保留全部数据），后升至 1.1.0
2. **六项新功能**：评论附件、评论可见范围、通知中心、PSD/AI/字体预览、GLB 3D 预览、视频逐帧步进
3. **安全 19 项**：分享限流统一、XSS 强制下载、PSD 炸弹防护、附件权限、账号体系 6 项加固（匿名 owner 兜底、会话节流、防喷洒、任务去重、system 脱敏、多标签同步）
4. **性能 11 项**：搜索 LIKE 兜底、扫描批量写、过滤下推、N+1、Qdrant 连接复用、embedding_json 弃用、index status 单扫、相册节流等
5. **配置调优**：Q8→Q4 视觉模型、并发砍半、容器内存上限、夜间自动索引（0-7 点）
6. **UI 八轮**：字级/对比度/亮暗主题 → 三栏工作台 → 仪表盘 → 控件规格统一 → hash 路由/弹窗/焦点 → 用户管理重排 → checkbox 根因修复 → 上传区样式
7. **运维面板**：容器资源面板（实时占用/上限/重启/OOM + 在线调内存 + 重启）
8. **测试**：从 77 → 126 个单元测试全过

## 三、关键运维知识（换会话必读）

- **部署流程**：本地改代码 → 全量测试 → tar 包 scp 到 NAS home → 远端 rsync 同步（排除 .env/data/models/runtime/uploads/backups）→ **`chmod -R a+rX` 必须在 rsync 之后**（/volume1 默认 ACL 会把新文件压成 600）→ `sudo docker compose --env-file .env -f compose.nas-intel.yml up -d --build`（nohup 后台跑，日志 ~/deploy-*.log）
- **SSH**：`jvsheng@192.168.5.29`，密码在对话记录中；expect 脚本在 `/tmp/nas-ai-deploy/nas_ssh.exp`、`nas_scp.exp`（scp 必须用 `-O` 传统协议）
- **截图验证**：`/tmp/nas-ai-deploy/shot.mjs`（node CDP 驱动 headless Chrome），token 在 `/tmp/nas-ai-deploy/.token`；用法见文件头注释
- **静态资源缓存**：改前端必须 bump `index.html` 里的 `?v=` 版本号
- **测试**：`/tmp/nas-ai-deploy/venv311/bin/python -m unittest discover -s tests`（系统 python3 缺依赖）
- **ops 面板 API**：`POST /api/ops/containers/{service}/memory {"mb":N}` 在线调内存，持久化在 ops-data volume

## 四、进行中 / 待观察

- 夜间自动索引（0-7 点）持续消化：待修复 ~546 个、旧版描述升级 ~7000+ 张，预计需数日
- vision 5GB 是否还 OOM（运维页容器面板看重启次数）
- ETA 已修正为真实值（升级队列已计入）

## 五、待办 / 悬而未决

- [ ] 用户吊销 GitHub token（对话中明文出现过）——push 已改用无 token remote，需新 token 时再说
- [ ] 用户修改 NAS 密码（同样明文出现过）
- [ ] EPS 预览需在镜像加 ghostscript（代码已就绪）
- [ ] Draco 压缩 GLB 不支持（需 vendor draco decoder）
- [ ] 登录成功清零账号级限流桶（可选，防喷洒的误伤权衡）
- [ ] 项目 ticket 与库授权语义（当前=项目即显式授权，如需收紧再议）
