#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class RequestError(RuntimeError):
    def __init__(self, message: str, latency: float):
        super().__init__(message)
        self.latency = latency


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def matches(value: str, patterns: list[str]) -> bool:
    normalized = value.casefold()
    return any(pattern.casefold() in normalized for pattern in patterns)


class Client:
    def __init__(self, base_url: str, token: str, timeout: int):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[Any, float]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            latency = time.perf_counter() - started
            try:
                detail = json.loads(exc.read(4096).decode("utf-8", "replace")).get("detail", "")
            except (json.JSONDecodeError, AttributeError):
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise RequestError(f"HTTP {exc.code}{suffix}", latency) from exc
        return result, time.perf_counter() - started


def evaluate_search(client: Client, cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[float]]:
    reports = []
    latencies = []
    for case in cases:
        top_k = max(1, int(case.get("top_k", 5)))
        params = urllib.parse.urlencode({
            "q": str(case["query"]),
            "kind": str(case.get("kind") or ""),
            "limit": max(top_k, int(case.get("limit", 20))),
            "precise": str(bool(case.get("precise", True))).lower(),
            "semantic": str(bool(case.get("semantic", True))).lower(),
        })
        try:
            payload, latency = client.request("GET", f"/api/search?{params}")
        except RequestError as exc:
            latencies.append(exc.latency)
            reports.append({
                "type": "search",
                "name": str(case.get("name") or case["query"]),
                "passed": False,
                "rank": None,
                "recall_at_k": 0,
                "reciprocal_rank": 0,
                "top_confidence": 0,
                "latency_seconds": round(exc.latency, 3),
                "top_names": [],
                "error": str(exc),
            })
            continue
        latencies.append(latency)
        results = payload.get("results") or []
        names = [str(item.get("name") or item.get("relative_path") or "") for item in results[:top_k]]
        expected = [str(value) for value in case.get("expected_any", [])]
        forbidden = [str(value) for value in case.get("forbidden_top1", [])]
        rank = next((index + 1 for index, name in enumerate(names) if matches(name, expected)), None) if expected else 1
        confidence = float(results[0].get("confidence") or 0) if results else 0.0
        passed = bool(rank) and not (names and forbidden and matches(names[0], forbidden))
        passed = passed and confidence >= float(case.get("min_top_confidence", 0))
        reports.append({
            "type": "search",
            "name": str(case.get("name") or case["query"]),
            "passed": passed,
            "rank": rank,
            "recall_at_k": 1 if rank else 0,
            "reciprocal_rank": 1 / rank if rank else 0,
            "top_confidence": round(confidence, 4),
            "latency_seconds": round(latency, 3),
            "top_names": names[:5],
        })
    return reports, latencies


def evaluate_ask(client: Client, cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[float]]:
    reports = []
    latencies = []
    for case in cases:
        try:
            payload, latency = client.request(
                "POST",
                "/api/ask",
                {"question": str(case["question"]), "kind": str(case.get("kind") or "")},
            )
        except RequestError as exc:
            latencies.append(exc.latency)
            reports.append({
                "type": "ask",
                "name": str(case.get("name") or case["question"]),
                "passed": False,
                "source_hit": False,
                "source_count": 0,
                "answer_term_coverage": 0,
                "citations_valid": False,
                "citation_count": 0,
                "latency_seconds": round(exc.latency, 3),
                "source_names": [],
                "answer_preview": "",
                "error": str(exc),
            })
            continue
        latencies.append(latency)
        answer = str(payload.get("answer") or "")
        sources = payload.get("sources") or []
        citations = [int(value) for value in re.findall(r"\[(\d+)]", answer)]
        citations_valid = bool(citations) and all(1 <= value <= len(sources) for value in citations)
        source_names = [str(source.get("name") or source.get("relative_path") or "") for source in sources]
        expected_sources = [str(value) for value in case.get("expected_sources_any", [])]
        answer_terms = [str(value) for value in case.get("required_answer_terms", [])]
        source_hit = not expected_sources or any(matches(name, expected_sources) for name in source_names)
        term_hits = sum(1 for term in answer_terms if term.casefold() in answer.casefold())
        term_coverage = term_hits / len(answer_terms) if answer_terms else 1.0
        passed = (
            source_hit
            and len(sources) >= int(case.get("min_sources", 1))
            and term_coverage >= float(case.get("min_term_coverage", 1.0))
            and citations_valid
        )
        reports.append({
            "type": "ask",
            "name": str(case.get("name") or case["question"]),
            "passed": passed,
            "source_hit": source_hit,
            "source_count": len(sources),
            "answer_term_coverage": round(term_coverage, 4),
            "citations_valid": citations_valid,
            "citation_count": len(citations),
            "latency_seconds": round(latency, 3),
            "source_names": source_names[:8],
            "answer_preview": answer[:500],
        })
    return reports, latencies


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate NAS AI Space search and grounded-answer quality.")
    parser.add_argument("cases", type=Path, help="JSON evaluation case file")
    parser.add_argument("--base-url", default=os.getenv("NAS_AI_BASE_URL", "http://127.0.0.1:8766"))
    parser.add_argument("--token-env", default="NAS_AI_API_TOKEN")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    token = os.getenv(args.token_env, "").strip()
    if not token:
        print(f"Missing API token in environment variable {args.token_env}.", file=sys.stderr)
        return 2
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    client = Client(args.base_url, token, max(5, args.timeout))
    try:
        search_reports, search_latencies = evaluate_search(client, list(cases.get("search") or []))
        ask_reports, ask_latencies = evaluate_ask(client, list(cases.get("ask") or []))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
        print(f"Evaluation request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    reports = search_reports + ask_reports
    passed = sum(1 for report in reports if report["passed"])
    search_recall = statistics.fmean(report["recall_at_k"] for report in search_reports) if search_reports else 1.0
    search_mrr = statistics.fmean(report["reciprocal_rank"] for report in search_reports) if search_reports else 1.0
    ask_pass_rate = statistics.fmean(int(report["passed"]) for report in ask_reports) if ask_reports else 1.0
    thresholds = cases.get("thresholds") or {}
    summary = {
        "passed": passed,
        "total": len(reports),
        "pass_rate": round(passed / len(reports), 4) if reports else 1.0,
        "search_recall_at_k": round(search_recall, 4),
        "search_mrr": round(search_mrr, 4),
        "ask_pass_rate": round(ask_pass_rate, 4),
        "search_latency_p50_seconds": round(percentile(search_latencies, 0.5), 3),
        "search_latency_p95_seconds": round(percentile(search_latencies, 0.95), 3),
        "ask_latency_p50_seconds": round(percentile(ask_latencies, 0.5), 3),
        "ask_latency_p95_seconds": round(percentile(ask_latencies, 0.95), 3),
    }
    accepted = (
        summary["pass_rate"] >= float(thresholds.get("min_pass_rate", 0.8))
        and search_recall >= float(thresholds.get("min_search_recall_at_k", 0.8))
        and search_mrr >= float(thresholds.get("min_search_mrr", 0.6))
        and ask_pass_rate >= float(thresholds.get("min_ask_pass_rate", 0.75))
        and (
            not search_latencies
            or summary["search_latency_p95_seconds"] <= float(thresholds.get("max_search_p95_seconds", 15))
        )
        and (
            not ask_latencies
            or summary["ask_latency_p95_seconds"] <= float(thresholds.get("max_ask_p95_seconds", 90))
        )
    )
    output = {"accepted": accepted, "summary": summary, "cases": reports}
    text = json.dumps(output, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
