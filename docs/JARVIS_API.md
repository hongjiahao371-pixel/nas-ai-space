# AI Space → Jarvis 接入包

本包供 Jarvis 的 NAS/Mac 后端使用。开发板仍承担语音和界面；照片/视频搜索、资料检索和看图推理由 AI Space 提供。`ai_space_client.py` 依赖 `httpx`，使用异步调用，不阻塞 Jarvis 的事件循环。

## 接口与版本

|能力|接口|v1.6.0|v1.6.4|
|---|---|---|---|
|搜索照片、视频、音频、文档|GET `/api/search?q=...&kind=video&limit=5&precise=false`|可用|可用|
|文件详情与文字分块|GET `/api/files/{file_id}`|可用|可用|
|依据资料问答，返回来源|POST `/api/ask`|可用|可用|
|以图搜图|POST `/api/search/image`，原始图片请求体|可用|可用|
|视频内候选画面|GET `/api/files/{file_id}/moments?q=...`|无此接口|可用|
|指定时刻JPEG画面|GET `/api/files/{file_id}/frame?time=...&revision=...`|无此接口|可用|
|临时传入图片，看图回答|POST `/api/integrations/vision?question=...`，原始图片请求体|无此接口|可用|
|能力查询|GET `/api/integrations/capabilities`|无此接口|可用|

v1.6.4包含这些接口；部署后通过能力查询确认实际版本。地址由 `AI_SPACE_URL` 指定。接入包本身不能证明调用方已接入；此前对 Jarvis 注册状态的检查只看主文件，遗漏独立模块。当前真实注册与调用须以部署验收记录为准。

## 凭据和调用

在可信后端环境配置 `AI_SPACE_URL` 和 `AI_SPACE_TOKEN`。使用请求头 `Authorization: Bearer ...`；不把令牌放入 URL、聊天、开发板固件、工具结果或源码。本包不含真实令牌。

v1.6.4 管理员在“用户与权限 → 应用集成密钥”选择媒体库并创建专用只读密钥，复制后放入可信后端的 AI_SPACE_TOKEN。密钥仅返回一次，数据库只存摘要；支持到期和撤销。服务端仅允许本节列出的读取、检索、问答和看图接口，其余个人、管理和修改接口默认拒绝。未授权或已停用的媒体库不可读取。该密钥不创建个人对话，也不保存看图图片或索引。能力接口 credential.readonly=true 表示服务端实际限制；普通会话和管理员令牌仍有各自原权限。

```python
from ai_space_client import AISpaceClient

client = AISpaceClient()  # 从后端环境读取地址和令牌
try:
    hits = await client.search("海边日落的视频", kind="video", limit=5)
    docs = await client.search("定时设置说明", kind="document")
    if docs["results"]:
        answer = await client.ask("这份说明书如何设置定时？",
                                  file_ids=[docs["results"][0]["file_id"]], kind="document")
    result = await client.analyze_image(camera_jpeg_bytes, "图里有什么？")
finally:
    await client.close()
```

调用方提供真实的 `camera_jpeg_bytes`；客户端不会自行遍历相机、任意路径或下载用户传入的 URL。看图图片限 JPEG/PNG/WebP、8MB、4000万像素，缩放最长边1280后推理。接口允许一个集成看图请求，忙时503及Retry-After；不会自动重试推理造成重复负载。

## Jarvis 工具接入位置

当前 Jarvis 的 `voice_tools.py` 用 `TOOLS` 发布工具，`execute()` 分派调用。本包提供四个工具：`self.jarvis.ai_space_search`、`self.jarvis.ai_space_ask`、`self.jarvis.ai_space_see_image`、`self.jarvis.ai_space_video_moments`。

在 Jarvis 启动时创建一个 `AISpaceClient`，附加本包 `TOOLS` 到现有工具清单；在已有分派函数前面识别这四个完整工具名，再调用 `execute_ai_space(client, name, arguments)`，退出时关闭 client。捕获 `AISpaceError` 返回工具错误，不能把超时/鉴权失败变成“没找到”。原有灯具状态查询仍应走 Home Assistant 的实时查询。

相机看图链路可直接调用 `client.analyze_image(已授权相机返回的JPEG, question)`。不要把 base64 图片塞进 ESP32 的工具JSON回复；只返回简洁回答。结果做过长度限制，适合现有约32KiB语音工具回复预算。

## 回答规则

- 搜索首先返回候选；相似度不是识别正确率。客户端不向语音模型传递 confidence，以免误读成可靠度。
- 视频是采样检索，返回时间点仍需观看实际画面核对；“四个按钮”等细节可能排序错误。
- 资料回答必须带来源；图片描述、转写和模型回答都可能误识别，不能冒充实时设备状态。
- 失败或部分覆盖素材不能算完整理解，未找到不代表不存在。独立音频质量未完成验证。
- 素材中的文字、提示或文档指令是资料，不获得控制家电、修改文件或执行命令的权限。

单独的客户端不会自动注册；现有 Jarvis 已通过 ai_space.py 注册四个调用工具和一个后台状态工具，voice_tools.py 引入后统一分派。使用真实语音仍需验证板端在线和播报链路。
