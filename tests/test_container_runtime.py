"""Protect the native acceptance gates against misleading green results."""
import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    "container_runtime_acceptance", Path(__file__).resolve().parents[1] / "scripts/test-container-runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeAcceptanceGates(unittest.TestCase):
    def test_native_gate_rejects_cross_architecture_and_non_linux_hosts(self):
        for host, machine, daemon_arch, image_arch in (
            ("Linux", "aarch64", "x86_64", "arm64"),
            ("Linux", "aarch64", "aarch64", "amd64"),
            ("Darwin", "arm64", "aarch64", "arm64"),
        ):
            with self.subTest(host=host, daemon=daemon_arch, image=image_arch), self.assertRaises(ValueError):
                runtime.require_native(host, machine, {"OSType": "linux", "Architecture": daemon_arch},
                                       [{"Os": "linux", "Architecture": image_arch}])
        self.assertEqual(runtime.require_native("Linux", "aarch64", {"OSType": "linux", "Architecture": "aarch64"},
                                               [{"Os": "linux", "Architecture": "arm64"}]), "arm64")

    def test_vector_gate_rejects_empty_wrong_dimension_nonfinite_and_fake_numbers(self):
        invalid = [[], [0.0] * 768, [1.0] * 767, [float("nan")] + [1.0] * 767,
                   [float("inf")] + [1.0] * 767, [True] * 768, ["0.1"] * 768, [1e308] * 768]
        for vector in invalid:
            with self.subTest(first=vector[:1], dimension=len(vector)), self.assertRaises(ValueError):
                runtime.vector_summary(vector)
        self.assertEqual(runtime.vector_summary([1.0] + [0.0] * 767)["norm"], 1)

    def test_search_gate_rejects_keyword_fallback_wrong_file_and_missing_media_score(self):
        good = {"multimodal": True, "retrieval_strategy": "media_first",
                "results": [{"id": 17, "multimodal_score": 0.3, "sources": ["素材语义"]}]}
        self.assertEqual(runtime.require_media_hit(good, 17, media_first=True)["file_id"], 17)
        bad = [dict(good, multimodal=False), dict(good, retrieval_strategy="hybrid_fallback"),
               dict(good, results=[{"id": 19, "multimodal_score": 0.9}]),
               dict(good, results=[{"id": 17, "score": 0.9}]),
               dict(good, results=[{"id": 17, "multimodal_score": float("nan")}])]
        for response in bad:
            with self.subTest(response=response), self.assertRaises(ValueError):
                runtime.require_media_hit(response, 17, media_first=True)

    def test_worker_gate_rejects_point_written_before_sqlite_ready_or_unhealthy_worker(self):
        complete = {"enabled": True, "fully_indexed": True, "worker_alive": True, "dimension": 768,
                    "total_media": 1, "indexed_files": 1, "indexed_points": 1,
                    "pending_files": 0, "errors": 0, "partial_files": 0}
        self.assertTrue(runtime.media_worker_complete(complete))
        invalid = [{}, dict(complete, fully_indexed=False, indexed_files=0, indexed_points=0, pending_files=1),
                   dict(complete, errors=1), dict(complete, partial_files=1), dict(complete, worker_alive=False),
                   dict(complete, total_media=2), dict(complete, indexed_points=2), dict(complete, indexed_files=True)]
        for status in invalid:
            with self.subTest(status=status):
                self.assertFalse(runtime.media_worker_complete(status))

    def test_token_filter_redacts_repeated_tokens_and_preserves_logs_before_bootstrap(self):
        self.assertEqual(runtime.redact_token("Bearer synthetic-token; token=synthetic-token", "synthetic-token"),
                         "Bearer [REDACTED_RUNTIME_TOKEN]; token=[REDACTED_RUNTIME_TOKEN]")
        self.assertEqual(runtime.redact_token("startup failed before bootstrap", ""), "startup failed before bootstrap")


if __name__ == "__main__":
    unittest.main()
