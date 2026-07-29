# 检索与问答质量验收

生产质量不能只凭一次人工搜索判断。项目提供 `scripts/evaluate-quality.py`，用固定真实样本持续测量：

- 搜索 Recall@K、MRR、首项置信度和 P50/P95 延迟
- 问答来源命中、最少来源数、答案关键事实覆盖率和 P50/P95 延迟
- 总通过率与可配置发布阈值

先复制样例并将占位文件名替换成当前媒体库中经过人工确认的真实文件名：

```bash
cp tests/quality-cases.example.json tests/quality-cases.local.json
export NAS_AI_API_TOKEN='从 NAS .env 读取，不要写入用例文件'
python3 scripts/evaluate-quality.py tests/quality-cases.local.json \
  --base-url http://NAS-IP:8766 \
  --output data/quality/latest.json
```

令牌只从环境变量读取，报告不会输出令牌。`quality-cases.local.json` 应保留在私有环境，不要把家庭文件名和问答样本发布到公共仓库。

建议至少维护 20 个搜索用例和 10 个问答用例，覆盖人物、物体组合、OCR、日期、视频台词、PDF 页码、Office 工作表、同义表达和无结果问题。每次更换视觉描述模板、Embedding 模型、精准重排提示词或融合排序权重后都运行同一套用例；只有指标达到阈值才部署。
