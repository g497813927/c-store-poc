from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from cstore_poc import run_all
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


if __name__ == "__main__":
    unittest.main()
