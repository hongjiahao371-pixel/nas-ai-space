from __future__ import annotations

import math
import re
import sqlite3
from typing import Any

from app.database import Database
from app.services.local_ai import LocalAIClient
from app.services.vectors import VectorStore


SYNONYMS = {
    "猫": ("猫咪", "小猫", "喵"),
    "猫咪": ("猫", "小猫", "喵"),
    "狗": ("狗狗", "小狗", "犬"),
    "狗狗": ("狗", "小狗", "犬"),
    "花盆": ("盆栽", "盆景", "植物盆"),
    "海边": ("海滩", "沙滩", "海岸", "海滨"),
    "日落": ("夕阳", "落日", "晚霞", "黄昏"),
    "人物": ("男士", "女士", "男性", "女性", "男子", "女人", "男孩", "女孩"),
    "聚会": ("派对", "宴会", "生日派对", "生日宴", "庆祝", "聚餐"),
    "聊天": ("对话", "消息", "微信"),
    "记录": ("纪录", "聊天记录"),
    "截图": ("屏幕", "界面", "截屏"),
    "生日": ("birthday", "happy birthday", "寿星"),
    "桌上": ("桌面", "桌前", "桌子上"),
    "发票": ("票据", "账单", "收据"),
    "汽车": ("车辆", "轿车", "车"),
    "宝宝": ("婴儿", "小孩", "儿童"),
    "结婚证": ("婚姻登记证", "结婚证明"),
}
STOP_PHRASES = (
    "共同出现的布置和物品", "共同出现了哪些布置和物品", "布置和物品", "布置与物品", "布置及物品",
    "共同出现了哪些布置", "共同出现的布置", "共同的布置元素", "共同布置元素", "共同的布置",
    "共同布置", "布置元素", "共同的", "相同的", "共同", "相同", "区别", "不同点", "对比", "比较",
    "请帮我", "帮我", "请问", "告诉我", "查找", "找到", "找出", "搜索", "看看", "相关的", "相关",
    "可以确认", "能够确认", "能否确认", "确认", "可以", "能否", "是否", "有哪些", "哪些", "什么",
    "能够看到", "可以看到", "能看到", "看到", "看见",
    "这些", "那些", "其中", "同时拍到了", "拍到了", "出现了", "出现", "同时",
    "拍摄的", "拍的", "显示的", "显示", "包含", "照片", "图片", "视频", "文件", "内容",
    "物品", "东西", "资料中", "资料里",
)
SCREENSHOT_QUERY_MARKERS = ("截图", "截屏", "屏幕", "界面", "聊天", "对话", "消息", "微信", "页面", "app")
SCREENSHOT_CONTENT_MARKERS = ("聊天界面", "微信界面", "手机屏幕", "电脑屏幕", "应用界面", "网页截图", "屏幕截图")


def _highlight(text: str, query: str, radius: int = 100) -> str:
    if not text:
        return ""
    index = text.lower().find(query.lower())
    if index < 0:
        return text[: radius * 2].strip()
    start = max(0, index - radius)
    end = min(len(text), index + len(query) + radius)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def _compact(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.lower(), flags=re.UNICODE)


def _partial_coverage_cap(coverage: float) -> float:
    if coverage >= 0.75:
        return 0.74
    if coverage >= 0.6:
        return 0.68
    if coverage >= 0.5:
        return 0.58
    if coverage > 0:
        return 0.45
    return 0.3


def _query_groups(query: str) -> list[tuple[str, ...]]:
    normalized = query.strip().lower()
    for phrase in STOP_PHRASES:
        normalized = normalized.replace(phrase, " ")
    normalized = re.sub(r"[的在里中和与]+", " ", normalized)
    parts = re.findall(r"[a-z0-9][a-z0-9._-]*|[\u4e00-\u9fff]+", normalized)
    terms: list[str] = []
    for part in parts:
        if re.fullmatch(r"[\u4e00-\u9fff]+", part) and len(part) >= 4:
            terms.extend(part[index:index + 2] for index in range(0, len(part), 2) if len(part[index:index + 2]) > 1)
        elif len(part) > 1:
            terms.append(part)
    groups: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for term in terms[:8]:
        if term in seen:
            continue
        aliases = tuple(dict.fromkeys((term, *SYNONYMS.get(term, ()))))
        groups.append(aliases)
        seen.add(term)
    return groups


def _fts_expression(query: str) -> str:
    # 复用 _query_groups 的停用词清理与同义词展开，把任意用户输入转成合法的 FTS5 表达式，
    # 供 MATCH 抛 OperationalError（未闭合引号、纯停用词等）时重试，而不是直接退到 LIKE 全表扫。
    terms = [alias for group in _query_groups(query) for alias in group]
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


# search() 结果构造实际使用的列（见 results 组装）。extracted_text 体积大（整篇文本），
# 不再随候选行物化，仅对进入最终排序页的候选单独补取（见 search() 中的回填逻辑）。
FILE_COLUMNS = "id, library_id, name, relative_path, kind, mime_type, size, mtime_ns, width, height, duration, ai_caption"
FILE_COLUMNS_F = ", ".join(f"f.{column}" for column in FILE_COLUMNS.split(", "))

# profile 对单条候选得分的最大影响：+0.28 覆盖 +0.18 精确 +0.08 文件名 +0.14 全覆盖奖励，
# 反向最多 -0.08 零覆盖 -0.06 截图惩罚；两者之和即任意候选相对截断线的最大可逆转分差。
PROFILE_MAX_SWING = 0.28 + 0.18 + 0.08 + 0.14 + 0.08 + 0.06


def _match_profile(
    row: dict[str, Any],
    groups: list[tuple[str, ...]],
    query: str,
    compact_groups: list[list[tuple[str, str]]] | None = None,
) -> dict[str, Any]:
    name = str(row.get("name") or "")
    path = str(row.get("relative_path") or "")
    caption = str(row.get("ai_caption") or "")
    content = str(row.get("extracted_text") or "")
    combined = _compact("\n".join((name, path, caption, content)))
    # 别名的 _compact 结果是查询级常量，由 search() 入口预算一次传入，
    # 避免在每行×每组×每别名上重复跑正则
    if compact_groups is None:
        compact_groups = [[(alias, _compact(alias)) for alias in group] for group in groups]
    matched_aliases: list[str] = []
    for compact_aliases in compact_groups:
        match = next(
            (alias for alias, compact in compact_aliases if compact and compact in combined),
            "",
        )
        if match:
            matched_aliases.append(match)
    coverage = len(matched_aliases) / len(groups) if groups else 0.0
    compact_query = _compact(query)
    exact = bool(compact_query and compact_query in combined)
    compact_name = _compact(name)
    name_match = any(
        compact and compact in compact_name
        for compact_aliases in compact_groups
        for _, compact in compact_aliases
    )
    return {
        "coverage": coverage,
        "matched": matched_aliases,
        "exact": exact,
        "name_match": name_match,
        "screenshot": name.lower().startswith(("screenshot", "screen_", "截屏", "截图"))
        or any(marker in caption or marker in content for marker in SCREENSHOT_CONTENT_MARKERS),
    }


class SearchService:
    def __init__(self, database: Database, ai: LocalAIClient, vectors: VectorStore):
        self.database = database
        self.ai = ai
        self.vectors = vectors

    def _files_by_ids(self, file_ids: list[int], columns: str = FILE_COLUMNS) -> dict[int, dict[str, Any]]:
        # 按 id 批量取回文件行建 dict，替代逐条 get_file（每条新开连接）的 N+1；
        # SQLite 单条语句变量上限 999，按 900 分批
        result: dict[int, dict[str, Any]] = {}
        unique = list(dict.fromkeys(int(file_id) for file_id in file_ids))
        for start in range(0, len(unique), 900):
            batch = unique[start:start + 900]
            placeholders = ",".join("?" for _ in batch)
            for row in self.database.fetchall(f"SELECT {columns} FROM files WHERE id IN ({placeholders})", batch):
                result[int(row["id"])] = row
        return result

    def _lexical(
        self,
        match_query: str,
        kind: str,
        limit: int,
        library_ids: list[int] | None = None,
        like_terms: list[str] | None = None,
        file_ids: list[int] | None = None,
        filter_sql: tuple[str, list[Any]] | None = None,
        allow_like: bool = True,
    ) -> list[dict[str, Any]]:
        filters = " AND f.kind = ?" if kind else ""
        params: list[Any] = [match_query]
        if kind:
            params.append(kind)
        if library_ids is not None:
            if not library_ids:
                return []
            filters += f" AND f.library_id IN ({','.join('?' for _ in library_ids)})"
            params.extend(library_ids)
        if file_ids is not None:
            if not file_ids:
                return []
            filters += f" AND f.id IN ({','.join('?' for _ in file_ids)})"
            params.extend(file_ids)
        if filter_sql is not None:
            clause, clause_params = filter_sql
            filters += f" AND ({clause})"
            params.extend(clause_params)
        params.append(limit)
        fts_sql = (
            f"""SELECT {FILE_COLUMNS_F}, bm25(files_fts, 7.0, 3.0, 1.0, 3.5) AS rank
                    FROM files_fts JOIN files f ON f.id = files_fts.rowid
                    WHERE files_fts MATCH ? {filters} ORDER BY rank LIMIT ?"""
        )
        try:
            rows = self.database.fetchall(fts_sql, params)
        except sqlite3.OperationalError:
            rows = []
            cleaned = _fts_expression(match_query)
            if cleaned and cleaned != match_query:
                try:
                    rows = self.database.fetchall(fts_sql, [cleaned, *params[1:]])
                except sqlite3.OperationalError:
                    rows = []
        if rows:
            return rows
        if not allow_like:
            return []

        # LIKE 兜底成本极高（模糊扫描整表），整个搜索过程只允许第一组执行一次。
        # 长度 >= 3 的词 FTS5 本身能覆盖内容列，兜底只扫 name/relative_path 两个短列；
        # 长度 < 3 的词（如双字中文）超出 trigram 的最小匹配长度，FTS 结构性无法命中，
        # 这些词的兜底必须保留 extracted_text/ai_caption，否则短词搜索会整体漏结果。
        terms = list(dict.fromkeys(term for term in (like_terms or [match_query]) if term))
        if not terms:
            return []
        clauses = []
        like_params: list[Any] = []
        for term in terms:
            pattern = f"%{term}%"
            if len(term) < 3:
                clauses.append("(f.name LIKE ? OR f.relative_path LIKE ? OR f.extracted_text LIKE ? OR f.ai_caption LIKE ?)")
                like_params.extend([pattern, pattern, pattern, pattern])
            else:
                clauses.append("(f.name LIKE ? OR f.relative_path LIKE ?)")
                like_params.extend([pattern, pattern])
        if kind:
            like_params.append(kind)
        if library_ids is not None:
            like_params.extend(library_ids)
        if file_ids is not None:
            like_params.extend(file_ids)
        if filter_sql is not None:
            like_params.extend(filter_sql[1])
        like_params.append(limit)
        return self.database.fetchall(
            f"""SELECT {FILE_COLUMNS_F}, 1.0 AS rank FROM files f WHERE ({' OR '.join(clauses)})
                {filters} ORDER BY f.mtime_ns DESC LIMIT ?""",
            like_params,
        )

    def search(
        self,
        query: str,
        kind: str = "",
        limit: int = 40,
        library_ids: list[int] | None = None,
        precise: bool = False,
        file_ids: list[int] | None = None,
        offset: int = 0,
        semantic: bool = True,
        filter_sql: tuple[str, list[Any]] | None = None,
    ) -> dict[str, Any]:
        page_size = max(1, limit)
        offset = max(0, offset)
        candidate_limit = max(80, min(1000, (page_size + offset) * 5))
        groups = _query_groups(query)
        # 别名集是查询级常量，_compact 结果预算一次传入 _match_profile，
        # 避免每行×每组×每别名重复正则
        compact_groups = [[(alias, _compact(alias)) for alias in group] for group in groups]
        screenshot_intent = any(marker in query.lower() for marker in SCREENSHOT_QUERY_MARKERS)
        rows: dict[int, dict[str, Any]] = {}
        allowed_file_ids = set(file_ids) if file_ids is not None else None
        scores: dict[int, float] = {}
        snippets: dict[int, str] = {}
        match_times: dict[int, float] = {}
        sources: dict[int, set[str]] = {}
        # 唯一的 LIKE 兜底挂在第一组上：词取原始查询 + 全部组的别名，
        # 后续各组（allow_like=False）不再各自触发全表模糊扫描。
        fallback_terms = [query, *(alias for group in groups for alias in group)]
        lexical_batches = [
            self._lexical(
                query, kind, candidate_limit, library_ids, fallback_terms, file_ids, filter_sql=filter_sql
            )
        ]
        for aliases in groups:
            expression = " OR ".join(f'"{alias.replace(chr(34), chr(34) * 2)}"' for alias in aliases)
            lexical_batches.append(
                self._lexical(
                    expression, kind, max(20, (limit + offset) * 2), library_ids, list(aliases), file_ids,
                    filter_sql=filter_sql, allow_like=False,
                )
            )

        for batch_index, batch in enumerate(lexical_batches):
            weight = 1.2 if batch_index == 0 else 0.75
            for position, row in enumerate(batch):
                file_id = int(row["id"])
                rows[file_id] = row
                scores[file_id] = scores.get(file_id, 0.0) + weight / (24 + position)
                sources.setdefault(file_id, set()).add("全文")
                if file_id not in snippets:
                    # extracted_text 已不随候选物化，此处先按 caption 生成；
                    # 进入最终排序页的候选会在回填文本后按原公式重算（见下方 profiled 循环）
                    text = row.get("extracted_text") or ""
                    snippets[file_id] = _highlight(
                        row["ai_caption"] or text,
                        next((alias for group in groups for alias in group if alias in (row["ai_caption"] or text)), query),
                    )

        semantic_used = False
        semantic_scores: dict[int, float] = {}
        if semantic and self.ai.settings.embedding_base_url and self.ai.settings.embedding_model:
            try:
                embedding_query = self.ai.embedding_query(query) if hasattr(self.ai, "embedding_query") else query
                embedding = self.ai.embeddings([embedding_query], interactive=True)[0]
                # 过滤条件下推：SQL 过滤（filter_sql）只在这里物化一次 id 列表供向量过滤用，
                # 不再拼进词法查询的 IN；超过 Qdrant 单批上限时按 2000 分批原生 filter，
                # 避免旧逻辑超过 2000 个 id 就静默放弃过滤导致的召回丢失。
                vector_id_filter: list[int] | None = None
                if file_ids is not None:
                    vector_id_filter = sorted({int(file_id) for file_id in file_ids})
                elif filter_sql is not None:
                    clause, clause_params = filter_sql
                    id_rows = self.database.fetchall(f"SELECT f.id FROM files f WHERE {clause}", clause_params)
                    allowed_file_ids = {int(row["id"]) for row in id_rows}
                    vector_id_filter = sorted(allowed_file_ids)
                hits: list[dict[str, Any]] = []
                if vector_id_filter is not None:
                    for start in range(0, len(vector_id_filter), 2000):
                        chunk = vector_id_filter[start:start + 2000]
                        if chunk:
                            hits.extend(self.vectors.search(embedding, candidate_limit, kind, library_ids, chunk))
                else:
                    hits = self.vectors.search(embedding, candidate_limit, kind, library_ids, None)
                best_hits: dict[int, dict[str, Any]] = {}
                for hit in hits:
                    payload = hit.get("payload", {})
                    file_id = int(payload["file_id"])
                    if allowed_file_ids is not None and file_id not in allowed_file_ids:
                        continue
                    if file_id not in best_hits or float(hit.get("score") or 0) > float(best_hits[file_id].get("score") or 0):
                        best_hits[file_id] = hit
                semantic = sorted(best_hits.values(), key=lambda hit: float(hit.get("score") or 0), reverse=True)
                semantic_used = True
                # 语义候选未命中词法结果时一次批量取回，替代逐条 get_file 的 N+1
                prefetched = self._files_by_ids(
                    [int(hit.get("payload", {})["file_id"]) for hit in semantic]
                )
                for position, hit in enumerate(semantic):
                    payload = hit.get("payload", {})
                    file_id = int(payload["file_id"])
                    if file_id not in rows:
                        row = prefetched.get(file_id)
                        if not row:
                            continue
                        if library_ids is not None and int(row["library_id"]) not in library_ids:
                            continue
                        rows[file_id] = row
                    similarity = max(0.0, min(1.0, float(hit.get("score") or 0)))
                    semantic_scores[file_id] = similarity
                    scores[file_id] = scores.get(file_id, 0.0) + 1.15 / (24 + position) + similarity * 0.18
                    sources.setdefault(file_id, set()).add("语义")
                    snippets[file_id] = payload.get("content", "")[:500]
                    if payload.get("source_label"):
                        sources.setdefault(file_id, set()).add(str(payload["source_label"]))
                    if payload.get("start_time") is not None and file_id not in match_times:
                        match_times[file_id] = float(payload["start_time"])
            except Exception:
                semantic_used = False

        profiles: dict[int, dict[str, Any]] = {}
        feedback_rows = self.database.fetchall(
            """SELECT file_id, verdict, COUNT(*) AS count FROM file_feedback
               WHERE query = ? GROUP BY file_id, verdict""",
            (query.strip(),),
        )
        feedback: dict[int, float] = {}
        for item in feedback_rows:
            file_id = int(item["file_id"])
            direction = 1 if item["verdict"] == "relevant" else -1 if item["verdict"] == "irrelevant" else 0
            feedback[file_id] = feedback.get(file_id, 0.0) + direction * min(0.2, 0.06 * int(item["count"]))
        # 先加与 profile 无关的分（多来源奖励、反馈），得到基础分排序
        for file_id in rows:
            score = scores.get(file_id, 0.0)
            if len(sources.get(file_id, set())) > 1:
                score += 0.03
            scores[file_id] = score + feedback.get(file_id, 0.0)
        # profile 只对可能进入最终排序页（含重排前 8 名）的候选计算：按基础分排序后，
        # 截断线为第 K 名基础分减去 profile 最大可逆转分差，线外候选无论如何进不了页面，
        # 跳过计算且不影响排序结果
        base_ordered = sorted(rows.values(), key=lambda row: scores.get(int(row["id"]), -math.inf), reverse=True)
        cutoff = max(offset + page_size, 8)
        threshold = (
            scores.get(int(base_ordered[cutoff - 1]["id"]), 0.0) - PROFILE_MAX_SWING
            if len(base_ordered) > cutoff else -math.inf
        )
        profiled_ids = {
            int(row["id"]) for row in base_ordered
            if scores.get(int(row["id"]), -math.inf) >= threshold
        }
        # 只为这些候选回填 extracted_text（profile 内容匹配与 snippet/evidence 需要），
        # 避免随全部候选物化整篇文本
        missing_text = [file_id for file_id in profiled_ids if "extracted_text" not in rows[file_id]]
        for file_id, row in self._files_by_ids(missing_text, "id, extracted_text").items():
            if file_id in rows:
                rows[file_id]["extracted_text"] = row["extracted_text"]
        for file_id in profiled_ids:
            row = rows[file_id]
            profile = _match_profile(row, groups, query, compact_groups)
            profiles[file_id] = profile
            score = scores.get(file_id, 0.0)
            score += profile["coverage"] * 0.28
            score += 0.18 if profile["exact"] else 0.0
            score += 0.08 if profile["name_match"] else 0.0
            if len(groups) > 1:
                if profile["coverage"] == 1:
                    score += 0.14
                elif profile["coverage"] == 0:
                    score -= 0.08
            if profile["screenshot"] and not screenshot_intent:
                score -= 0.06
            scores[file_id] = score
            if file_id not in semantic_scores:
                # 纯词法行的 snippet 在合并阶段缺少 extracted_text，这里按原公式用完整文本重算；
                # 语义行的 snippet 来自向量 payload，不依赖 extracted_text
                text = row.get("extracted_text") or ""
                snippets[file_id] = _highlight(
                    row["ai_caption"] or text,
                    next((alias for group in groups for alias in group if alias in (row["ai_caption"] or text)), query),
                )

        ordered = sorted(rows.values(), key=lambda row: scores.get(int(row["id"]), -math.inf), reverse=True)
        rerank_scores: dict[int, float] = {}
        rerank_reasons: dict[int, str] = {}
        precise_used = False
        if precise and ordered:
            candidates = []
            for row in ordered[:8]:
                file_id = int(row["id"])
                candidates.append({
                    "id": file_id,
                    "name": row["name"],
                    "path": row["relative_path"],
                    "kind": row["kind"],
                    "content": snippets.get(file_id) or row["ai_caption"] or row.get("extracted_text") or "",
                })
            try:
                reranked = self.ai.rerank(query, candidates)
                for file_id, item in reranked.items():
                    if file_id in scores:
                        profile = profiles.get(file_id, {})
                        coverage = float(profile.get("coverage") or 0)
                        reason = str(item.get("reason") or "")
                        rerank_score = float(item["score"])
                        if len(groups) > 1:
                            rerank_score *= 0.45 + 0.55 * coverage
                            if coverage < 1:
                                rerank_score = min(rerank_score, _partial_coverage_cap(coverage))
                            if any(marker in reason for marker in ("未显示", "未明确", "无关", "不符合", "只满足", "仅")):
                                rerank_score = min(rerank_score, 0.45)
                        if profile.get("screenshot") and not screenshot_intent:
                            rerank_score *= 0.85
                        rerank_scores[file_id] = rerank_score
                        rerank_reasons[file_id] = reason
                        scores[file_id] = scores[file_id] * 0.35 + rerank_score * 0.65
                        sources.setdefault(file_id, set()).add("精准重排")
                precise_used = bool(rerank_scores)
                ordered = sorted(rows.values(), key=lambda row: scores.get(int(row["id"]), -math.inf), reverse=True)
            except Exception:
                precise_used = False

        total_candidates = len(ordered)
        selected = ordered[offset:offset + page_size]
        raw_values = [scores.get(int(row["id"]), 0.0) for row in selected]
        high = max(raw_values, default=1.0)
        low = min(raw_values, default=0.0)
        results = []
        for row in selected:
            file_id = int(row["id"])
            if high > low:
                confidence = 0.2 + 0.75 * (scores[file_id] - low) / (high - low)
            else:
                confidence = min(0.95, max(0.05, scores.get(file_id, 0.0)))
            profile = profiles.get(file_id, {})
            if precise_used and len(groups) > 1 and float(profile.get("coverage") or 0) < 1:
                confidence = min(confidence, _partial_coverage_cap(float(profile.get("coverage") or 0)))
            if precise_used and any(
                marker in rerank_reasons.get(file_id, "")
                for marker in ("未显示", "未明确", "无关", "不符合", "只满足", "仅")
            ):
                confidence = min(confidence, 0.45)
            results.append({
                "id": file_id,
                "name": row["name"],
                "path": row["relative_path"],
                "kind": row["kind"],
                "mime_type": row["mime_type"],
                "size": row["size"],
                "mtime_ns": row["mtime_ns"],
                "width": row["width"],
                "height": row["height"],
                "duration": row["duration"],
                "caption": row["ai_caption"],
                "snippet": snippets.get(file_id, ""),
                "evidence": snippets.get(file_id) or row["ai_caption"] or row.get("extracted_text") or "",
                "match_time": match_times.get(file_id),
                "source_label": next(
                    (value for value in sources.get(file_id, set()) if value not in {"全文", "语义", "精准重排"}),
                    "",
                ),
                "sources": sorted(sources.get(file_id, set())),
                "score": scores.get(file_id, 0.0),
                "confidence": max(0.0, min(1.0, confidence)),
                "matched_terms": profiles.get(file_id, {}).get("matched", []),
                "coverage": float(profiles.get(file_id, {}).get("coverage") or 0),
                "rerank_reason": rerank_reasons.get(file_id, ""),
                "semantic_score": semantic_scores.get(file_id),
            })
        return {
            "query": query,
            "semantic": semantic_used,
            "precise": precise_used,
            "terms": [group[0] for group in groups],
            "total": total_candidates,
            "offset": offset,
            "limit": page_size,
            "has_more": offset + len(selected) < total_candidates,
            "results": results,
        }

    def similar(
        self,
        file_id: int,
        kind: str = "",
        limit: int = 20,
        library_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        vector = self.vectors.representative_vector(file_id)
        if not vector:
            return {"file_id": file_id, "semantic": False, "total": 0, "results": []}
        hits = self.vectors.search(vector, max(80, limit * 5), kind, library_ids)
        # 候选一次批量取回（SELECT * 保持原有完整字段），替代逐条 get_file 的 N+1
        prefetched = self._files_by_ids(
            [
                candidate_id
                for hit in hits
                if (candidate_id := int((hit.get("payload") or {}).get("file_id") or 0)) and candidate_id != file_id
            ],
            "*",
        )
        rows: list[tuple[dict[str, Any], float, dict[str, Any]]] = []
        seen = {file_id}
        for hit in hits:
            payload = hit.get("payload") or {}
            candidate_id = int(payload.get("file_id") or 0)
            if not candidate_id or candidate_id in seen:
                continue
            row = prefetched.get(candidate_id)
            if not row:
                continue
            seen.add(candidate_id)
            rows.append((row, max(0.0, min(1.0, float(hit.get("score") or 0))), payload))
            if len(rows) >= limit:
                break
        results = []
        for row, similarity, payload in rows:
            results.append({
                "id": int(row["id"]),
                "name": row["name"],
                "path": row["relative_path"],
                "kind": row["kind"],
                "mime_type": row["mime_type"],
                "size": row["size"],
                "mtime_ns": row["mtime_ns"],
                "width": row["width"],
                "height": row["height"],
                "duration": row["duration"],
                "caption": row["ai_caption"],
                "snippet": str(payload.get("content") or row["ai_caption"] or row["extracted_text"])[:500],
                "evidence": str(payload.get("content") or row["ai_caption"] or row["extracted_text"])[:500],
                "match_time": payload.get("start_time"),
                "sources": ["相似内容"],
                "score": similarity,
                "confidence": similarity,
                "semantic_score": similarity,
                "matched_terms": [],
                "coverage": 1.0,
                "rerank_reason": "",
            })
        return {"file_id": file_id, "semantic": True, "total": len(results), "results": results}
