from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from cstore_poc import DEFAULT_RANDOM_SEED, build_parser, random_profile, run_all
from reporting import create_report


ROOT = Path(__file__).resolve().parents[1]


class CStorePocSmokeTest(unittest.TestCase):
    def test_small_synthetic_run(self) -> None:
        profile = json.loads((ROOT / "mock_profile.json").read_text(encoding="utf-8"))
        profile = copy.deepcopy(profile)
        profile["complete_row_count"] = 2_000
        profile["distinct_uid_count"] = 1_200
        profile["topic_frequencies_ranked"] = [1_500, 500]
        profile["status_frequencies"] = {"0": 1_800, "1": 200}
        profile["description_length_bytes"] = {
            "min": 0, "p25": 12, "median": 20, "p75": 32,
            "p90": 48, "p99": 80, "max": 120, "mean": 25.0,
        }
        profile["data_length_bytes"] = {
            "min": 60, "p25": 90, "median": 120, "p75": 160,
            "p90": 200, "p99": 280, "max": 400, "mean": 135.0,
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            results = root / "results"
            run_all(profile, work, results)
            create_report(results / "cstore_poc_summary.json", results)
            summary = json.loads((results / "cstore_poc_summary.json").read_text(encoding="utf-8"))

            self.assertTrue(summary["correctness"].startswith("PASS"))
            self.assertEqual(12, len(summary["benchmark_results"]))
            self.assertEqual(4, len(summary["layout_sizes"]))
            self.assertTrue((results / "benchmark_latency.png").exists())
            self.assertTrue((results / "layout_size.png").exists())
            self.assertTrue((results / "REPORT.md").exists())

    def test_checked_in_profile_excludes_source_identifiers(self) -> None:
        profile = json.loads((ROOT / "mock_profile.json").read_text(encoding="utf-8"))
        forbidden = {"source_database", "source_database_sha256", "source_path", "topic_names", "ids"}
        self.assertTrue(forbidden.isdisjoint(profile))
        self.assertIn("aggregate shape only", profile["privacy_mode"])

    def test_report_handles_latency_below_default_log_scale_floor(self) -> None:
        summary = json.loads((ROOT / "demo_results" / "cstore_poc_summary.json").read_text(encoding="utf-8"))
        summary["benchmark_results"][0]["median_ms"] = 0.001

        with tempfile.TemporaryDirectory() as directory:
            results = Path(directory)
            summary_path = results / "cstore_poc_summary.json"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            create_report(summary_path, results)

            self.assertTrue((results / "benchmark_latency.svg").exists())
            self.assertTrue((results / "benchmark_latency.png").exists())

    def test_random_profile_is_reproducible_and_self_contained(self) -> None:
        first = random_profile(5_000, 12, 42)
        second = random_profile(5_000, 12, 42)
        self.assertEqual(first, second)
        self.assertEqual(5_000, sum(first["topic_frequencies_ranked"]))
        self.assertEqual(12, len(first["topic_frequencies_ranked"]))
        self.assertEqual(5_000, sum(first["status_frequencies"].values()))
        self.assertIn("no uploaded database", first["privacy_mode"])

    def test_random_cli_uses_current_default_seed(self) -> None:
        args = build_parser().parse_args(["all", "--random"])
        self.assertEqual(20260923, DEFAULT_RANDOM_SEED)
        self.assertEqual(DEFAULT_RANDOM_SEED, args.seed)


if __name__ == "__main__":
    unittest.main()
