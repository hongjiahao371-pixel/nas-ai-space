# 小型素材库的检索内存配置

此配置面向内存充足、约数万向量的小型素材库。两套向量的原始大小可按点数乘以维度乘以四字节估算，实际使用还包括素材属性、图索引、运行时与优化临时空间。默认限制原始向量总量为 256 MiB；不是总内存用量承诺。

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
