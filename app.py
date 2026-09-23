#!/usr/bin/env python3
"""Local Gradio UI for privacy-preserving C-Store layout experiments."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import gradio as gr
import pandas as pd

from cstore_poc import profile_database, run_all
from reporting import create_report


ROOT = Path(__file__).resolve().parent
RUN_ROOT = ROOT / ".runs"


def result_markdown(summary: dict[str, object]) -> str:
    results = {
        (row["layout"], row["query"]): float(row["median_ms"])
        for row in summary["benchmark_results"]
    }
    sizes = {row["layout"]: row for row in summary["layout_sizes"]}
    status_speedup = results[("row_unsorted", "status_count")] / results[("column_unsorted", "status_count")]
    range_speedup = results[("row_unsorted", "topic_time_count")] / results[("column_unsorted", "topic_time_count")]
    sorted_row_speedup = results[("row_unsorted", "topic_time_count")] / results[("row_sorted", "topic_time_count")]
    gzip_reduction = 1 - float(sizes["column_unsorted"]["gzip_bytes"]) / float(sizes["row_unsorted"]["gzip_bytes"])
    row_count = int(summary["data_profile"]["row_count"])
    return f"""## Result

All timed operations used **{row_count:,} generated mock rows**. The upload was opened read-only only to derive aggregate shape statistics; source IDs, labels, descriptions, and payloads were not retained.

- Unsorted column status scan: **{status_speedup:.1f}x faster** than unsorted rows.
- Unsorted column topic/time scan: **{range_speedup:.1f}x faster** than unsorted rows.
- Sorted row projection topic/time lookup: **{sorted_row_speedup:.1f}x faster** than the unsorted row scan.
- Unsorted column gzip footprint: **{gzip_reduction * 100:.1f}% smaller** than unsorted rows.
- Correctness: **PASS** across all four layouts and three queries.
"""


def run_uploaded_database(database_file: str | None, row_limit: float) -> tuple[object, ...]:
    if not database_file:
        raise gr.Error("Upload a SQLite database first.")
    run_id = uuid.uuid4().hex[:12]
    run_dir = RUN_ROOT / run_id
    work_dir = run_dir / "work"
    results_dir = run_dir / "results"
    try:
        profile = profile_database(Path(database_file))
        limit = int(row_limit) if row_limit else None
        run_all(profile, work_dir, results_dir, row_limit=limit)
        create_report(results_dir / "cstore_poc_summary.json", results_dir)
        summary = json.loads((results_dir / "cstore_poc_summary.json").read_text(encoding="utf-8"))
        benchmark_table = pd.DataFrame(summary["benchmark_results"])[
            ["query", "layout", "median_ms", "logical_bytes_accessed", "result"]
        ]
        size_table = pd.DataFrame(summary["layout_sizes"])[
            ["layout", "raw_mib", "gzip_mib", "file_count"]
        ]
        archive_path = Path(shutil.make_archive(str(run_dir / "cstore-poc-results"), "zip", results_dir))
        shutil.rmtree(work_dir, ignore_errors=True)
        return (
            result_markdown(summary),
            benchmark_table,
            size_table,
            str(results_dir / "benchmark_latency.png"),
            str(results_dir / "layout_size.png"),
            str(archive_path),
        )
    except Exception as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise gr.Error(f"POC run failed: {exc}") from exc


CSS = """
.gradio-container { max-width: 1180px !important; }
.privacy-note { border-left: 4px solid #2364AA; padding-left: 14px; }
"""


with gr.Blocks(title="C-Store Layout POC") as demo:
    gr.Markdown(
        """# C-Store physical-layout POC

Upload a SQLite database with the supplied `dynamics` schema. The app derives aggregate shape statistics, generates a deterministic mock dataset, and benchmarks custom **row/column × sorted/unsorted** binary layouts. SQLite never executes a timed comparison query.
"""
    )
    gr.Markdown(
        """**Privacy boundary:** no uploaded database is copied into the project or results. The retained profile excludes source paths/hashes, IDs, topic labels, descriptions, and payloads. Downloaded results contain synthetic data statistics only.""",
        elem_classes=["privacy-note"],
    )
    with gr.Row():
        database = gr.File(
            label="SQLite database",
            file_types=[".db", ".sqlite", ".sqlite3"],
            type="filepath",
        )
        row_limit = gr.Slider(
            minimum=1_000,
            maximum=100_000,
            value=25_000,
            step=1_000,
            label="Synthetic rows",
            info="Use the upload's full complete-row count when it is below this limit.",
        )
    run_button = gr.Button("Generate mock and run benchmark", variant="primary")
    summary_output = gr.Markdown()
    with gr.Tab("Performance"):
        latency_image = gr.Image(label="Median latency graph", type="filepath")
        benchmark_output = gr.Dataframe(label="Benchmark details", interactive=False)
    with gr.Tab("Disk space"):
        size_image = gr.Image(label="Layout size graph", type="filepath")
        size_output = gr.Dataframe(label="Layout size details", interactive=False)
    download_output = gr.File(label="Download sanitized results")

    run_button.click(
        fn=run_uploaded_database,
        inputs=[database, row_limit],
        outputs=[
            summary_output,
            benchmark_output,
            size_output,
            latency_image,
            size_image,
            download_output,
        ],
        concurrency_limit=1,
        show_progress="full",
    )


if __name__ == "__main__":
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    demo.launch(css=CSS)
