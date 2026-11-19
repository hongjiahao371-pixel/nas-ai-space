# 网页选区摘录：两步本地原型

基线 PR #22 `eb8ea3f59b23704c381ac873369ce7e946e57fd2`。没有推送、部署或更改 UGREENlink。

## 真正可用范围
1. 在 Chromium 开发者扩展中加载 `integrations/web-capture`，主动选中文本并打开扩展；再点击读取选区。预览和修改标题、来源 URL、正文后显式保存 JSON。扩展只声明 activeTab/scripting，没有主机权限、后台采集、NAS 凭据、cookies 或网络发送。
2. NAS AI Space 中进入目标项目 → 入库箱 → 选择该 JSON。页面展示目标项目与完整选区，用户确认后仅导入该文件到项目 inbox 并建立素材、排队单文件索引。不是全程一键，也不是整页快照；不抓图片、附件、表单或 iframe。无需把会话令牌交给扩展。

入库为普通 `.txt`，正文含 JSON 形式来源信息、浏览器提供时间与正文 SHA-256；项目素材描述也展示来源。来源/标题/时间可被用户编辑，属于未核验的自述信息，不是可信电子取证。URL 默认去 query/hash，含用户名密码的 URL 拒绝；路径和选区本身仍可能含隐私。目标项目成员可以访问导入内容，上传前必须确认授权范围。

## 安全边界
- 服务端只接收 <=256KiB UTF-8 JSON；严格schema/字段类型/重复key/字符/URL/UTC时间限制。拒绝HTML包、图片伪装、凭据URL、查询参数与fragment。正文作为纯文本，不执行 HTML/脚本；不解析网页指令。
- 来源 URL 从不做服务端请求、DNS解析、重定向追踪或资源下载，无通用 URL 抓取接口。后续索引仍沿用项目原有AI配置，和来源网页无关。
- 项目编辑权限和启用的上传媒体库权限在接收前及接收完毕后检查。沿用现有会话/CSRF机制，不增加公开或宽松CORS入口。
- 以目录fd+O_DIRECTORY/O_NOFOLLOW遍历入库目录；叶文件使用O_EXCL和O_NOFOLLOW临时写、fsync、硬链接发布。已有文件拒绝symlink/FIFO/修改，内容地址幂等，不覆盖人工修改的素材描述。
- 单个素材创建在SQLite事务中去重。文件发布与数据库非跨资源事务：数据库失败可能留下已保存但未注册的inbox txt，重试可恢复，现有收集也可能发现它。返回201仅在文件和素材都建立之后；索引排队失败会返回task_id=null，而不是声称已索引。
- 本地/NAS管理员仍是文件系统信任边界：目录可被有权限的外部进程重命名；后续既有scanner/indexer按路径读取，不提供对恶意并发NAS管理员的完整TOCTOU隔离。普通远程导入请求无路径控制。没有跨用户磁盘配额/大规模吞吐保证。

## 验证状态
- 7项Python测试（含多子用例）：真实API→inbox→素材→索引文本块、重复/人工元数据保持、限长/无效内容、鉴权/编辑角色/库权限、接收后权限复核、路径symlink、写失败清理、索引队列失败状态。
- 扩展5项Node纯函数/manifest约束测试、JS语法和Python编译检查。
- 浏览器扩展实际权限授予、文本读取、本地下载，以及NAS UI桌面/移动布局和点击闭环尚待真实浏览器验收；未把纯函数测试称浏览器通过。
- 无真实网页/个人资料被采集，无真实NAS被修改。

权限设计对照 Chrome 官方文档：
- https://developer.chrome.com/docs/extensions/develop/concepts/activeTab
- https://developer.chrome.com/docs/extensions/reference/api/scripting

扩展采用拒绝带账号密码的来源URL（而不是默默发送或保留），合法URL在导出前移除query/hash。后端亦拒绝未清理的URL；不从来源获取任何资源。
