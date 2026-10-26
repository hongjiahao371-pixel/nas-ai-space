# 小型素材库的检索内存配置

此配置仅支持 Linux Docker，面向内存充足、约数万向量的小型素材库。两套向量的原始大小可按点数乘以维度乘以四字节估算，实际使用还包括素材属性、图索引、运行时与优化临时空间。默认限制原始向量总量为 256 MiB；不是总内存用量承诺。

1. 创建并校验包含数据库和两套向量的恢复包。保留原 Compose 文件和检索服务镜像。
2. 在隔离检索服务恢复快照，先运行下面的脚本，核对所有向量与属性的前后摘要及代表查询结果。
3. 设置 `NAS_AI_SEARCH_MEMORY_PROFILE=true`，管理脚本会在安装、升级和重启时将 `compose.search-memory.yml` 加在 Compose 文件列表最后。手工使用 Compose 时也须保留该文件。设置 `NAS_AI_VECTORS_ON_DISK=false`，并在已有多模态索引器环境中传入该变量。重建检索服务，已有数据卷保持不变。
4. 对生产检索服务运行脚本。`--backup-manifest` 必须指向已验证恢复包的 JSON 清单；每次使用一个新报告文件名。

```sh
python scripts/vector-memory.py --url http://qdrant:6333 \
  --collection nas_ai_chunks --collection nas_ai_media_gemma2_q8_v1 \
  --backup-manifest data/backups/recovery/RECOVERY_NAME.json \
  --report data/backups/vector-memory-REPORT.json --apply
```

不传 `--apply` 时只检查并输出摘要。运行脚本需要可访问检索服务的可信环境；不要公开 Qdrant 的内部端口。迁移期间避免索引写入，否则摘要不一致会终止验收，保留报告等待核对，不会删除或覆盖向量。

回退：对每个集合使用报告里的 `rollback_patch` 调用 Qdrant `PATCH /collections/{name}`，移除新增 Compose 覆盖文件、恢复原环境配置并重建检索服务。配置回退不替换数据；只有数据损坏时才使用经校验的完整恢复包。

保持原有备份优先级和带宽，完成并行检索验收。若出现内存不足、持续超时或备份失败，不应关闭问题。此配置改变内存与磁盘的取舍，不改变模型、向量维度、权限或结果含义。

依据：[Qdrant 1.18 集合参数更新接口](https://api.qdrant.tech/v-1-18-x/api-reference/collections/update-collection)。


### 保持检索页驻留

Qdrant 的 `on_disk: false` 仍使用可被系统回收的文件映射缓存，不代表永久锁定内存。该可选配置增加 `qdrant-memory` 只读辅助服务，按实际数据区间锁定检索页，跳过稀疏文件预留的空洞。预算固定 384 MiB，容器上限 512 MiB；两者都计入整机容量规划。它不修改原文件或向量，关闭即可释放，不替代持久化卷和备份。

辅助服务每十秒发现文件替换、增长和删除，释放旧页并重新检查。实际数据超过预算、系统不允许锁页或检索文件尚不存在时显示未就绪，检索服务仍使用原数据工作；此时不能宣称性能保护已经生效。不要为了通过验收无限增加内存预算。

该配置另提供 `database-memory`，只读驻留明确指定的 SQLite 主库及当前 WAL；预算 128 MiB、容器上限 192 MiB，原数据库路径和 FULL 持久化方式不变。它以 `NAS_UID`/`NAS_GID` 运行，须与数据目录所有者一致；权限不足应修正服务账号，不扩大数据库权限。每十秒检查变化，WAL 截断后在同一 inode 重新增长也会重新锁页；这不是任意时刻全部新页已驻留的保证。主库缺失、指向符号链接、超过预算或锁页失败时显示未就绪。

自建镜像必须包含 `/usr/local/bin/nas-ai-pin-search-pages.py`，并通过 `NAS_AI_APP_IMAGE` 让两个辅助服务使用新镜像。升级后核对 `qdrant-memory`、`database-memory` 的健康状态以及日志中的 `locked_bytes`、`required_bytes` 和 `errors`，再做真实备份并行测试。两个辅助容器上限合计 704 MiB，不含 Qdrant 自身 1 GiB 或应用内存。移除内存配置时先停止并移除两个辅助服务，原检索及数据库卷保留。

内存语义依据：[Qdrant 内存层级及旧参数映射](https://qdrant.tech/documentation/ops-configuration/memory-tiers/)。
