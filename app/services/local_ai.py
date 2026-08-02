from __future__ import annotations

import ast
import base64
import ipaddress
import json
import mimetypes
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import httpx
from PIL import Image

from app.config import Settings


def _parse_rerank_values(content: str) -> list[dict]:
    start = content.find("[")
    end = content.rfind("]")
    if start < 0:
        return []
    fragment = content[start:end + 1] if end > start else content[start:]
    if end > start:
        for parser in (json.loads, ast.literal_eval):
            try:
                values = parser(fragment)
                return values if isinstance(values, list) else []
            except (json.JSONDecodeError, SyntaxError, ValueError):
                continue
    values = []
    for match in re.finditer(r"\{[^{}]+\}", fragment):
        for parser in (json.loads, ast.literal_eval):
            try:
                item = parser(match.group(0))
                if isinstance(item, dict):
                    values.append(item)
                break
            except (json.JSONDecodeError, SyntaxError, ValueError):
                continue
    return values


def _parse_json_object(content: str) -> dict:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return {}
    fragment = content[start:end + 1]
    for parser in (json.loads, ast.literal_eval):
        try:
            value = parser(fragment)
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, SyntaxError, ValueError):
            continue
    return {}


def _render_common_answer(payload: dict, sources: list[dict], question: str) -> str:
    items = payload.get("common") or payload.get("common_points") or []
    if not isinstance(items, list):
        items = []
    excluded = ()
    if "布置" in question:
        excluded = (
            "男子", "女子", "男士", "女士", "人物", "西装", "衣着", "动作", "微笑", "氛围",
            "坐在", "站在", "斜倚",
        )
    lines: list[str] = []
    seen: set[str] = set()
    placeholders = {"共同事实", "具体事实", "事实", "共同点", "布置元素", "内容", "物品", "元素"}
    for item in items:
        if not isinstance(item, dict):
            continue
        text = re.sub(r"\s+", " ", str(item.get("text") or item.get("claim") or "")).strip(" -。；")[:80]
        if not text or text in placeholders or any(marker in text for marker in excluded):
            continue
        references = []
        for value in item.get("sources", []):
            try:
                reference = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= reference <= len(sources) and reference not in references:
                references.append(reference)
        if len(references) < 2:
            continue
        key = re.sub(r"[\W_]+", "", text.lower(), flags=re.UNICODE)
        if not key or key in seen:
            continue
        seen.add(key)
        citations = "".join(f"[{reference}]" for reference in references[:8])
        lines.append(f"- {text} {citations}")
        if len(lines) >= 8:
            break
    if not lines:
        return "现有证据不足以由至少两份资料可靠确认共同点。"
    return "由至少两份资料共同支持的内容：\n\n" + "\n".join(lines)


class _PriorityGate:
    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.active = 0
        self.interactive_waiters = 0
        self.condition = threading.Condition()

    @contextmanager
    def slot(self, interactive: bool = False) -> Iterator[None]:
        with self.condition:
            if interactive:
                self.interactive_waiters += 1
            try:
                while self.active >= self.limit or (not interactive and self.interactive_waiters):
                    self.condition.wait()
                self.active += 1
            finally:
                if interactive:
                    self.interactive_waiters -= 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()


class LocalAIClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client_lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._set_gates(1)

    def _http(self) -> httpx.Client:
        # 复用同一 httpx.Client（连接池），避免每次推理请求重建 TCP/TLS 连接；
        # httpx.Client 线程安全，懒初始化；各请求按需传 timeout 覆盖默认值
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
        return self._client

    def _set_gates(self, limit: int) -> None:
        gates: dict[str, _PriorityGate] = {}

        def gate(endpoint: str) -> _PriorityGate:
            key = endpoint.rstrip("/")
            if key not in gates:
                gates[key] = _PriorityGate(limit)
            return gates[key]

        self._embedding_gate = gate(self.settings.embedding_base_url)
        self._vision_gate = gate(self.settings.vision_base_url)
        self._chat_gate = gate(self.settings.chat_base_url)
        self._transcription_gate = gate(self.settings.transcription_base_url)

    def set_max_concurrency(self, value: int) -> None:
        self._set_gates(max(1, value))

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.local_ai_base_url
            or self.settings.embedding_base_url
            or self.settings.vision_base_url
            or self.settings.chat_base_url
        )

    def _validate_endpoint(self, base_url: str | None = None) -> str:
        endpoint = (base_url or self.settings.local_ai_base_url).rstrip("/")
        if not endpoint:
            raise RuntimeError("尚未配置本地模型服务")
        if self.settings.allow_cloud_endpoints:
            return endpoint
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RuntimeError("模型服务地址无效")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 80)}
        except socket.gaierror as exc:
            raise RuntimeError("无法解析模型服务地址") from exc
        if not all(ipaddress.ip_address(address).is_private or ipaddress.ip_address(address).is_loopback for address in addresses):
            raise RuntimeError("默认仅允许访问局域网或本机模型服务")
        return endpoint

    @staticmethod
    def _api_url(base_url: str, route: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith(("/v1", "/v3")):
            return f"{base}/{route.lstrip('/')}"
        return f"{base}/v1/{route.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.local_ai_api_key:
            headers["Authorization"] = f"Bearer {self.settings.local_ai_api_key}"
        return headers

    def health(self) -> dict:
        endpoints = {
            "embedding": self.settings.embedding_base_url,
            "vision": self.settings.vision_base_url,
            "chat": self.settings.chat_base_url,
        }
        endpoints = {key: value for key, value in endpoints.items() if value}
        if not endpoints:
            return {"configured": False, "reachable": False}
        def check(value: str) -> dict:
            try:
                endpoint = self._validate_endpoint(value)
                response = self._http().get(self._api_url(endpoint, "models"), headers=self._headers(), timeout=3)
                return {"reachable": response.is_success, "status": response.status_code}
            except Exception as exc:
                return {"reachable": False, "error": str(exc)}

        unique_endpoints = list(dict.fromkeys(endpoints.values()))
        with ThreadPoolExecutor(max_workers=len(unique_endpoints)) as executor:
            results = dict(zip(unique_endpoints, executor.map(check, unique_endpoints)))
        statuses = {key: dict(results[value]) for key, value in endpoints.items()}
        return {
            "configured": True,
            "reachable": all(item["reachable"] for item in statuses.values()),
            "endpoints": statuses,
        }

    def embeddings(self, texts: list[str], interactive: bool = False) -> list[list[float]]:
        if not self.settings.embedding_model or not texts:
            return []
        endpoint = self._validate_endpoint(self.settings.embedding_base_url)
        with self._embedding_gate.slot(interactive):
            response = self._http().post(
                self._api_url(endpoint, "embeddings"),
                headers=self._headers(),
                json={"model": self.settings.embedding_model, "input": texts},
                timeout=120,
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            return [item["embedding"] for item in data]

    @staticmethod
    def embedding_query(query: str) -> str:
        return (
            "Instruct: Retrieve private photos, videos, audio and documents that satisfy all subjects, "
            "attributes, scenes, actions, time references and visible text in the user's query.\n"
            f"Query: {query}"
        )

    def caption_image(self, path: Path) -> str:
        if not self.settings.vision_model:
            return ""
        endpoint = self._validate_endpoint(self.settings.vision_base_url)
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        screenshot = path.name.lower().startswith(("screenshot", "screen_", "截屏", "截图"))
        try:
            with Image.open(path) as image:
                width, height = image.size
        except (OSError, ValueError):
            width = height = 0
        low_resolution = bool(width and height and (min(width, height) < 160 or width * height < 40_000))
        if screenshot:
            prompt = (
                "请先逐项读取清晰文字，再用一段不超过220个汉字的中文客观描述这张屏幕截图。"
                "准确写出应用或页面名称、关键消息原文、时间、金额、文件名、扩展名、大小、型号和状态。"
                "聊天中只有顶部标题可作为联系人或群名；文件卡片里的文字必须按文件名和文件信息描述，"
                "不得误写成联系人。区分界面文字、头像与聊天中图片，不要用省略号替代可辨文字；看不清则明确说明。"
            )
        elif low_resolution:
            prompt = (
                f"这张图分辨率只有{width}×{height}。请用一段不超过80个汉字的中文，"
                "只描述能够明确看清的颜色、形状、局部物体和文字；无法确认主体或场景时必须明确说明，"
                "禁止猜测人物、地点、动作和用途。"
            )
        else:
            prompt = (
                "请用一段不超过180个汉字的中文客观描述图片。覆盖主要人物或动物数量与外观、衣着、"
                "物体类别和颜色、动作、物体之间的位置关系、室内外场景、背景环境以及清晰可见的品牌和文字。"
                "只写直接可见的事实，不推测人物身份、具体地点、用途、情绪或事件；看不清就明确说明。"
            )
        prompt += "不要写标题、字段、列表或检索词，不要重复同一个词语或句子，不要使用空泛评价。"
        payload = {
            "model": self.settings.vision_model,
            "temperature": 0,
            "max_tokens": 260,
            "repeat_penalty": 1.15,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ],
            }],
        }
        with self._vision_gate.slot():
            response = self._http().post(
                self._api_url(endpoint, "chat/completions"),
                headers=self._headers(),
                json=payload,
                timeout=180,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
        content = re.sub(r"\s+", " ", content).strip()
        content = re.sub(r"^(图片|画面|描述)[:：]\s*", "", content)
        sentences = re.findall(r"[^。！？]+[。！？]?", content)
        content = "".join(
            sentence for sentence in sentences
            if not (
                any(marker in sentence for marker in ("整体画面", "整体构图", "氛围"))
                and any(marker in sentence for marker in ("简洁", "柔和", "温馨", "舒适", "美观", "和谐"))
            )
        ).strip()
        if len(content) > 360:
            end = max(content.rfind(mark, 0, 360) for mark in ("。", "！", "？", "；"))
            content = content[:end + 1] if end > 80 else content[:360]
        return content

    def ocr_document_page(self, path: Path, page_number: int) -> str:
        if not self.settings.vision_model:
            return ""
        endpoint = self._validate_endpoint(self.settings.vision_base_url)
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        prompt = (
            f"这是文档第{page_number}页的扫描图。请按阅读顺序准确转写所有清晰可见文字，"
            "保留标题、段落、编号、日期、金额和表格中的行列关系；表格用简洁 Markdown 表示。"
            "不要概括、解释或补全模糊内容，看不清的位置写[无法辨认]。"
        )
        payload = {
            "model": self.settings.vision_model,
            "temperature": 0,
            "max_tokens": 1200,
            "repeat_penalty": 1.08,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ],
            }],
        }
        with self._vision_gate.slot():
            response = self._http().post(
                self._api_url(endpoint, "chat/completions"),
                headers=self._headers(),
                json=payload,
                timeout=240,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()

    def rerank(self, query: str, candidates: list[dict]) -> dict[int, dict]:
        if not self.settings.chat_model or not candidates:
            return {}
        endpoint = self._validate_endpoint(self.settings.chat_base_url)
        documents = "\n\n".join(
            f"[{index}] 文件名：{candidate['name']}\n路径：{candidate['path']}\n"
            f"类型：{candidate['kind']}\n内容：{str(candidate.get('content') or '')[:320]}"
            for index, candidate in enumerate(candidates, 1)
        )
        payload = {
            "model": self.settings.chat_model,
            "temperature": 0,
            "max_tokens": 520,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是严格的私人资料相关性重排器。查询包含多个条件时，默认这些条件需要同时满足，"
                        "不能因为只命中一个词就给高分。只依据候选内容中明确出现的主体、属性、场景、动作和文字评分，"
                        "不得脑补。90-100表示直接且完整满足，70-89表示高度相关但缺少次要条件，"
                        "40-69表示只满足部分条件，0-39表示弱相关或无关。"
                        "完整满足全部条件的候选必须高于只满足部分条件的候选。"
                        "只返回JSON数组，每项格式为{\"id\":候选编号,\"score\":0到100,\"reason\":\"12字内理由\"}。"
                    ),
                },
                {"role": "user", "content": f"查询：{query}\n\n候选：\n{documents}"},
            ],
        }
        with self._chat_gate.slot(interactive=True):
            response = self._http().post(
                self._api_url(endpoint, "chat/completions"),
                headers=self._headers(),
                json=payload,
                timeout=180,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
        values = _parse_rerank_values(content)
        result: dict[int, dict] = {}
        for item in values:
            try:
                index = int(item["id"]) - 1
                candidate = candidates[index]
                score = max(0.0, min(1.0, float(item["score"]) / 100))
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            result[int(candidate["id"])] = {"score": score, "reason": str(item.get("reason") or "")[:80]}
        return result

    def answer(self, question: str, sources: list[dict], history: list[dict] | None = None) -> str:
        if not self.settings.chat_model:
            raise RuntimeError("尚未配置本地问答模型")
        endpoint = self._validate_endpoint(self.settings.chat_base_url)
        context = "\n\n".join(
            f"[{index}] 文件：{source['path']}\n匹配度：{float(source.get('confidence') or 0):.0%}\n"
            f"证据：{str(source.get('evidence') or source.get('snippet') or source.get('caption') or '')[:1000]}"
            for index, source in enumerate(sources, 1)
        )
        if any(marker in question for marker in ("共同", "相同", "都有哪些", "都有什么", "都有")):
            payload = {
                "model": self.settings.chat_model,
                "temperature": 0,
                "max_tokens": 500,
                "repeat_penalty": 1.15,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "只输出一个JSON对象，根字段为common；common数组中的每项只能包含text和sources。"
                            "text必须是从证据中提取的具体物品或具体事实，sources是支持它的资料编号数组。"
                            "严禁把‘共同事实’、‘具体事实’、‘共同点’、‘布置元素’、‘内容’等说明文字写入text。"
                            "common最多8项；每项必须在至少两个不同编号的证据中分别明确出现。"
                            "只写问题所问类别，布置元素不包括人物、服装、动作或氛围。"
                            "颜色、材质、位置也必须在每个所列来源中明确，不能推断或迁移属性。"
                            "仅单个来源出现或不确定的内容不要输出。"
                        ),
                    },
                    {"role": "user", "content": f"资料：\n{context}\n\n问题：{question}"},
                ],
            }
            answer = ""
            for _ in range(2):
                with self._chat_gate.slot(interactive=True):
                    response = self._http().post(
                        self._api_url(endpoint, "chat/completions"),
                        headers=self._headers(),
                        json=payload,
                        timeout=180,
                    )
                    response.raise_for_status()
                    content = response.json()["choices"][0]["message"]["content"].strip()
                answer = _render_common_answer(_parse_json_object(content), sources, question)
                if not answer.startswith("现有证据不足"):
                    break
                payload["messages"].extend([
                    {"role": "assistant", "content": content[:1000]},
                    {
                        "role": "user",
                        "content": "上一次输出无有效具体事实。请重新核对每份证据，text只能填写证据原文明确出现的具体物品或事实；没有则输出空数组。",
                    },
                ])
            return answer
        messages = [
            {
                "role": "system",
                "content": (
                    "你是严谨的私有 NAS 资料助手。先判断资料是否真正回答问题，只使用直接相关的证据。"
                    "多个来源重复时合并，不把相似图片数量当成独立事实。每个可核查事实紧跟 [编号]；"
                    "若证据只支持部分结论，明确区分“可以确认”“可能”“无法确认”。"
                    "回答共同点、相同点或比较类问题时，每个声称为共同点的事实必须至少由两个不同编号明确支持；"
                    "共同点段落和最终总结都只能列满足该条件的事实。只在一份资料出现的内容如有必要，"
                    "必须放在单独的“仅单个来源出现”段落，绝不能混入共同点。最多列8项，避免重复展开引用原文。"
                    "严格遵守问题所问类别：询问布置元素时，人物、衣着、动作和氛围不属于布置元素。"
                    "问题限定位置、数量或时间时，证据必须明确建立该关系；“场景中出现”不能推成“桌上、旁边或里面有”。"
                    "颜色、材质和位置等属性必须在每个所引来源中分别明确，不能把一个来源的属性迁移到另一个来源。"
                    "与问题核心主体或场景不匹配的来源必须完全忽略，不要为了说明其无关而列出其中的物品。"
                    "不得根据文件名、常识或低相关资料补全缺失信息，不得编造路径、人物身份、时间和地点。"
                    "引用格式只写[1]、[2]，不要写“编号”。"
                ),
            },
        ]
        for message in (history or [])[-6:]:
            role = str(message.get("role") or "")
            if role in {"user", "assistant"}:
                messages.append({"role": role, "content": str(message.get("content") or "")[:2000]})
        messages.append({"role": "user", "content": f"本轮检索资料：\n{context}\n\n本轮问题：{question}"})
        payload = {
            "model": self.settings.chat_model,
            "temperature": 0.1,
            "max_tokens": 800,
            "repeat_penalty": 1.15,
            "messages": messages,
        }
        with self._chat_gate.slot(interactive=True):
            response = self._http().post(
                self._api_url(endpoint, "chat/completions"),
                headers=self._headers(),
                json=payload,
                timeout=180,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()

    def transcribe(self, path: Path) -> dict:
        if not self.settings.transcription_base_url or not self.settings.transcription_model:
            return {"text": "", "segments": []}
        endpoint = self._validate_endpoint(self.settings.transcription_base_url)
        headers = self._headers()
        headers.pop("Content-Type", None)
        with self._transcription_gate.slot():
            with path.open("rb") as handle:
                response = self._http().post(
                    self._api_url(endpoint, "audio/transcriptions"),
                    headers=headers,
                    data={"model": self.settings.transcription_model, "response_format": "verbose_json"},
                    files={"file": (path.name, handle, "audio/mpeg")},
                    timeout=3600,
                )
                response.raise_for_status()
                payload = response.json()
        return {"text": payload.get("text", ""), "segments": payload.get("segments", [])}
