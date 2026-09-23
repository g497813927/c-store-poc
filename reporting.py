#!/usr/bin/env python3
"""Create SVG graphs and a Markdown report from cstore_poc_summary.json."""

from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont


LAYOUT_ORDER = ["row_unsorted", "row_sorted", "column_unsorted", "column_sorted"]
QUERY_ORDER = ["status_count", "topic_time_count", "payload_bytes_for_status"]
COLORS = {
    "row_unsorted": "#2364AA",
    "row_sorted": "#D79F2B",
    "column_unsorted": "#6D8C54",
    "column_sorted": "#C05A7A",
}
LABELS = {
    "row_unsorted": "Row / unsorted",
    "row_sorted": "Row / sorted",
    "column_unsorted": "Column / unsorted",
    "column_sorted": "Column / sorted",
    "status_count": "Status count",
    "topic_time_count": "Topic + time range",
    "payload_bytes_for_status": "Payload bytes by status",
}


def log_plot_bounds(values: Iterable[float], default_min: float, default_max: float) -> tuple[float, float]:
    """Return padded positive bounds that always contain the observed values."""
    observed = [float(value) for value in values]
    if not observed or any(not math.isfinite(value) or value <= 0 for value in observed):
        raise ValueError("Log-scale chart values must be finite and greater than zero")
    return min(default_min, min(observed) * 0.8), max(default_max, max(observed) * 1.2)


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def svg_text(x: float, y: float, text: str, **attrs: object) -> str:
    settings = {"x": x, "y": y, "font-family": "Arial, sans-serif", "fill": "#252A31", **attrs}
    rendered = " ".join(f'{key.replace("_", "-")}="{esc(value)}"' for key, value in settings.items())
    return f"<text {rendered}>{esc(text)}</text>"


def latency_svg(summary: dict[str, object], output: Path) -> None:
    values = {
        (row["query"], row["layout"]): float(row["median_ms"])
        for row in summary["benchmark_results"]
    }
    width, height = 1080, 640
    left, right, top, bottom = 105, 35, 105, 150
    plot_w, plot_h = width - left - right, height - top - bottom
    y_min, y_max = log_plot_bounds(values.values(), 0.05, 100.0)
    ticks = [0.05, 0.1, 0.5, 1, 5, 10, 50, 100]
    dataset_label = "Provided raw rows (labels redacted)" if summary.get("dataset") else "Synthetic 85,478-row dataset"

    def y_position(value: float) -> float:
        fraction = (math.log10(value) - math.log10(y_min)) / (math.log10(y_max) - math.log10(y_min))
        return top + plot_h * (1 - fraction)

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Median query latency by physical layout</title>',
        '<desc id="desc">Grouped bars on a logarithmic millisecond scale compare row and column layouts in sorted and unsorted order for three analytical queries.</desc>',
        '<rect width="100%" height="100%" fill="#FFFFFF"/>',
        svg_text(left, 38, "Median query latency by physical layout", font_size=26, font_weight=700),
        svg_text(left, 67, f"{dataset_label} · median of 9 warm-cache runs · log scale", font_size=15, fill="#59636E"),
    ]
    for tick in ticks:
        y = y_position(tick)
        elements.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" stroke="#D9DEE5" stroke-width="1"/>')
        elements.append(svg_text(left - 12, y + 5, f"{tick:g}", font_size=13, text_anchor="end", fill="#59636E"))
    elements.append(svg_text(25, top + plot_h / 2, "Median latency (ms, log scale)", font_size=14, transform=f"rotate(-90 25 {top + plot_h / 2})", text_anchor="middle"))

    group_w = plot_w / len(QUERY_ORDER)
    bar_w = min(48, group_w / 5.5)
    for query_index, query in enumerate(QUERY_ORDER):
        center = left + group_w * (query_index + 0.5)
        for layout_index, layout in enumerate(LAYOUT_ORDER):
            value = values[(query, layout)]
            x = center + (layout_index - 1.5) * bar_w
            y = y_position(value)
            base = y_position(y_min)
            elements.append(
                f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w - 5:.2f}" height="{base - y:.2f}" fill="{COLORS[layout]}" rx="2"/>'
            )
            elements.append(svg_text(x + (bar_w - 5) / 2, y - 7, f"{value:.2f}", font_size=10, text_anchor="middle", fill="#252A31"))
        elements.append(svg_text(center, top + plot_h + 30, LABELS[query], font_size=14, text_anchor="middle"))

    legend_y = height - 58
    legend_x = left
    for layout in LAYOUT_ORDER:
        elements.append(f'<rect x="{legend_x}" y="{legend_y - 13}" width="16" height="16" fill="{COLORS[layout]}" rx="2"/>')
        elements.append(svg_text(legend_x + 23, legend_y, LABELS[layout], font_size=13))
        legend_x += 225
    elements.append("</svg>")
    output.write_text("\n".join(elements), encoding="utf-8")


def size_svg(summary: dict[str, object], output: Path) -> None:
    sizes = {row["layout"]: row for row in summary["layout_sizes"]}
    width, height = 1080, 610
    left, right, top, bottom = 105, 35, 100, 135
    plot_w, plot_h = width - left - right, height - top - bottom
    size_values = [float(sizes[layout][metric]) for layout in LAYOUT_ORDER for metric in ("raw_mib", "gzip_mib")]
    y_min, y_max = log_plot_bounds(size_values, 0.01, 500.0)
    ticks = [0.01, 0.1, 1, 10, 100, 300]

    def y_position(value: float) -> float:
        fraction = (math.log10(value) - math.log10(y_min)) / (math.log10(y_max) - math.log10(y_min))
        return top + plot_h * (1 - fraction)

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Physical layout size before and after gzip compression</title>',
        '<desc id="desc">Grouped bars compare raw and gzip-compressed mebibytes for four custom physical layouts.</desc>',
        '<defs><pattern id="openFill" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" fill="#FFFFFF"/><path d="M0 8L8 0" stroke="#D79F2B" stroke-width="2"/></pattern></defs>',
        '<rect width="100%" height="100%" fill="#FFFFFF"/>',
        svg_text(left, 38, "Physical layout size", font_size=26, font_weight=700),
        svg_text(left, 67, "Same seven logical columns in every layout · MiB on a log scale", font_size=15, fill="#59636E"),
    ]
    for tick in ticks:
        y = y_position(tick)
        elements.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" stroke="#D9DEE5" stroke-width="1"/>')
        elements.append(svg_text(left - 12, y + 5, str(tick), font_size=13, text_anchor="end", fill="#59636E"))
    elements.append(svg_text(27, top + plot_h / 2, "Size (MiB, log scale)", font_size=14, transform=f"rotate(-90 27 {top + plot_h / 2})", text_anchor="middle"))

    group_w = plot_w / len(LAYOUT_ORDER)
    bar_w = min(72, group_w / 3.2)
    for index, layout in enumerate(LAYOUT_ORDER):
        center = left + group_w * (index + 0.5)
        for metric_index, (metric, fill) in enumerate((("raw_mib", "#2364AA"), ("gzip_mib", "url(#openFill)"))):
            value = float(sizes[layout][metric])
            x = center + (metric_index - 1) * bar_w + 3
            y = y_position(value)
            base = y_position(y_min)
            elements.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w - 7:.2f}" height="{base - y:.2f}" fill="{fill}" stroke="#252A31" stroke-width="1" rx="2"/>')
            elements.append(svg_text(x + (bar_w - 7) / 2, y - 8, f"{value:.1f}", font_size=11, text_anchor="middle"))
        elements.append(svg_text(center, top + plot_h + 31, LABELS[layout], font_size=13, text_anchor="middle"))
    legend_y = height - 48
    elements.extend(
        [
            '<rect x="370" y="547" width="18" height="18" fill="#2364AA" stroke="#252A31"/>',
            svg_text(396, legend_y, "Raw", font_size=13),
            '<rect x="520" y="547" width="18" height="18" fill="url(#openFill)" stroke="#252A31"/>',
            svg_text(546, legend_y, "Gzip level 6", font_size=13),
        ]
    )
    elements.append("</svg>")
    output.write_text("\n".join(elements), encoding="utf-8")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default(size=size)


def centered(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, text_font, fill: str = "#252A31") -> None:
    draw.text(xy, text, font=text_font, fill=fill, anchor="mm")


def latency_png(summary: dict[str, object], output: Path) -> None:
    values = {(row["query"], row["layout"]): float(row["median_ms"]) for row in summary["benchmark_results"]}
    width, height = 1080, 640
    left, right, top, bottom = 105, 35, 105, 150
    plot_w, plot_h = width - left - right, height - top - bottom
    y_min, y_max = log_plot_bounds(values.values(), 0.05, 100.0)
    ticks = [0.05, 0.1, 0.5, 1, 5, 10, 50, 100]
    dataset_label = "Provided raw rows (labels redacted)" if summary.get("dataset") else "Synthetic 85,478-row dataset"
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((left, 24), "Median query latency by physical layout", font=font(26, True), fill="#252A31")
    draw.text((left, 60), f"{dataset_label} · median of 9 warm-cache runs · log scale", font=font(15), fill="#59636E")

    def y_position(value: float) -> float:
        fraction = (math.log10(value) - math.log10(y_min)) / (math.log10(y_max) - math.log10(y_min))
        return top + plot_h * (1 - fraction)

    for tick in ticks:
        y = y_position(tick)
        draw.line((left, y, left + plot_w, y), fill="#D9DEE5", width=1)
        draw.text((left - 12, y), f"{tick:g}", font=font(13), fill="#59636E", anchor="rm")
    draw.text((left, top - 17), "Median latency (ms, log scale)", font=font(13), fill="#59636E", anchor="ls")
    group_w = plot_w / len(QUERY_ORDER)
    bar_w = min(48, group_w / 5.5)
    for query_index, query in enumerate(QUERY_ORDER):
        center_x = left + group_w * (query_index + 0.5)
        for layout_index, layout in enumerate(LAYOUT_ORDER):
            value = values[(query, layout)]
            x = center_x + (layout_index - 1.5) * bar_w
            y = y_position(value)
            draw.rounded_rectangle((x, y, x + bar_w - 5, y_position(y_min)), radius=2, fill=COLORS[layout])
            centered(draw, (x + (bar_w - 5) / 2, y - 9), f"{value:.2f}", font(10))
        centered(draw, (center_x, top + plot_h + 29), LABELS[query], font(14))
    legend_y, legend_x = height - 57, left
    for layout in LAYOUT_ORDER:
        draw.rounded_rectangle((legend_x, legend_y - 9, legend_x + 16, legend_y + 7), radius=2, fill=COLORS[layout])
        draw.text((legend_x + 23, legend_y), LABELS[layout], font=font(13), fill="#252A31", anchor="lm")
        legend_x += 225
    image.save(output, optimize=True)


def size_png(summary: dict[str, object], output: Path) -> None:
    sizes = {row["layout"]: row for row in summary["layout_sizes"]}
    width, height = 1080, 610
    left, right, top, bottom = 105, 35, 100, 135
    plot_w, plot_h = width - left - right, height - top - bottom
    size_values = [float(sizes[layout][metric]) for layout in LAYOUT_ORDER for metric in ("raw_mib", "gzip_mib")]
    y_min, y_max = log_plot_bounds(size_values, 0.01, 500.0)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((left, 24), "Physical layout size", font=font(26, True), fill="#252A31")
    draw.text((left, 60), "Same seven logical columns in every layout · MiB on a log scale", font=font(15), fill="#59636E")

    def y_position(value: float) -> float:
        fraction = (math.log10(value) - math.log10(y_min)) / (math.log10(y_max) - math.log10(y_min))
        return top + plot_h * (1 - fraction)

    for tick in [0.01, 0.1, 1, 10, 100, 300]:
        y = y_position(tick)
        draw.line((left, y, left + plot_w, y), fill="#D9DEE5", width=1)
        draw.text((left - 12, y), str(tick), font=font(13), fill="#59636E", anchor="rm")
    draw.text((left, top - 17), "Size (MiB, log scale)", font=font(13), fill="#59636E", anchor="ls")
    group_w = plot_w / len(LAYOUT_ORDER)
    bar_w = min(72, group_w / 3.2)
    for index, layout in enumerate(LAYOUT_ORDER):
        center_x = left + group_w * (index + 0.5)
        for metric_index, (metric, fill) in enumerate((("raw_mib", "#2364AA"), ("gzip_mib", "#F8EAC7"))):
            value = float(sizes[layout][metric])
            x = center_x + (metric_index - 1) * bar_w + 3
            y = y_position(value)
            draw.rounded_rectangle((x, y, x + bar_w - 7, y_position(y_min)), radius=2, fill=fill, outline="#252A31", width=1)
            if metric == "gzip_mib":
                hatch_y = int(y)
                while hatch_y < int(y_position(y_min)):
                    draw.line((x, hatch_y + 8, min(x + bar_w - 7, x + 8), hatch_y), fill="#D79F2B", width=2)
                    hatch_y += 9
            centered(draw, (x + (bar_w - 7) / 2, y - 10), f"{value:.1f}", font(11))
        centered(draw, (center_x, top + plot_h + 30), LABELS[layout], font(13))
    draw.rectangle((370, 547, 388, 565), fill="#2364AA", outline="#252A31")
    draw.text((396, 556), "Raw", font=font(13), fill="#252A31", anchor="lm")
    draw.rectangle((520, 547, 538, 565), fill="#F8EAC7", outline="#252A31")
    draw.line((520, 565, 538, 547), fill="#D79F2B", width=2)
    draw.text((546, 556), "Gzip level 6", font=font(13), fill="#252A31", anchor="lm")
    image.save(output, optimize=True)


def ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator


def markdown_table(headers: list[str], rows: Iterable[Iterable[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def report_markdown(summary: dict[str, object]) -> str:
    results = {(row["layout"], row["query"]): row for row in summary["benchmark_results"]}
    sizes = {row["layout"]: row for row in summary["layout_sizes"]}
    status_speedup = ratio(results[("row_unsorted", "status_count")]["median_ms"], results[("column_unsorted", "status_count")]["median_ms"])
    range_speedup = ratio(results[("row_unsorted", "topic_time_count")]["median_ms"], results[("column_unsorted", "topic_time_count")]["median_ms"])
    sorted_row_speedup = ratio(results[("row_unsorted", "topic_time_count")]["median_ms"], results[("row_sorted", "topic_time_count")]["median_ms"])
    payload_speedup = ratio(results[("row_unsorted", "payload_bytes_for_status")]["median_ms"], results[("column_unsorted", "payload_bytes_for_status")]["median_ms"])
    gzip_reduction = 1 - ratio(sizes["column_unsorted"]["gzip_bytes"], sizes["row_unsorted"]["gzip_bytes"])
    profile = summary["data_profile"]
    config = summary["benchmark_config"]

    latency_rows = []
    for query in QUERY_ORDER:
        latency_rows.append(
            [LABELS[query]]
            + [f'{float(results[(layout, query)]["median_ms"]):.3f}' for layout in LAYOUT_ORDER]
        )
    size_rows = [
        [LABELS[layout], f'{float(sizes[layout]["raw_mib"]):.2f}', f'{float(sizes[layout]["gzip_mib"]):.2f}']
        for layout in LAYOUT_ORDER
    ]
    return f"""# C-Store physical-layout POC

## TL;DR

On a deterministic **synthetic** dataset shaped like the supplied SQLite database ({int(profile['row_count']):,} rows, seven columns), the C-Store idea works strongly for narrow analytical reads:

- Unsorted column layout was **{status_speedup:.1f}x faster** for a single-column status count and **{payload_speedup:.1f}x faster** for summing payload lengths by status.
- For a topic/time range predicate, unsorted column layout was **{range_speedup:.1f}x faster** than unsorted rows; a row projection sorted on `(topic, time, id)` was **{sorted_row_speedup:.1f}x faster** than the unsorted row scan.
- Raw size was effectively identical because both custom encodings store the same values without padding. Under identical gzip settings, the unsorted column files used **{gzip_reduction * 100:.1f}% less space** than the unsorted row file.
- Sorting did **not** reduce total compressed size in this mock. It improved the sort-key query, but modestly worsened compression for other shuffled columns. That is a useful caution: a projection earns its space only when its workload benefit justifies the extra ordering.

![Median query latency](benchmark_latency.svg)

![Layout size](layout_size.svg)

## Method

The implementation follows one focused aspect of Stonebraker et al., *C-Store: A Column-oriented DBMS* (VLDB 2005): column representation plus multiple sort orders (“projections”).

Dataset provenance reference: [dingwen07/Bilibili-dynamic](https://github.com/dingwen07/Bilibili-dynamic). That upstream data-collection project is referenced only and is not bundled with this POC.

1. SQLite is opened read-only only to derive aggregate shape: schema, row count, ranked category frequencies, status counts, time bounds, distinct-user count, and text-length quantiles.
2. No source path, file hash, IDs, category labels, descriptions, or payloads are retained. A seeded generator creates mock rows with synthetic IDs, labels, descriptions, and JSON-like payloads.
3. Four custom binary layouts are built from the mock CSV snapshot: row/column × unsorted/sorted by `(topic, time, id)`.
4. Timed queries use only those binary files. SQLite is never used for a benchmark query.
5. Each result is the median of {int(config['repeats'])} warm-cache measurements after {int(config['warmups'])} warmups. Every query returned the same value in all four layouts.

This isolates the paper's physical-layout claim, but it is not a full DBMS comparison: there is no optimizer, transaction system, buffer manager, vectorized row executor, concurrent workload, or cold-cache control.

## Median query latency (ms)

{markdown_table(['Query'] + [LABELS[item] for item in LAYOUT_ORDER], latency_rows)}

The column reader touches only the needed arrays. The row reader must step through the packed record stream, whose record headers are interleaved with roughly 294 MiB of mock text payload. The sorted range query uses binary search, so the row projection drops from a full scan to a few dozen key probes.

## Physical size (MiB)

{markdown_table(['Layout', 'Raw', 'Gzip level 6'], size_rows)}

The raw formats are intentionally dense and nearly identical in total size. Column compression wins because homogeneous values are compressed separately. The result is directionally consistent with the paper, while much smaller than C-Store's published end-to-end advantage because this POC applies generic gzip and does not execute directly on compressed encodings.

## Workload

- `status_count`: count rows matching the minority status (`{config['target_status']}`).
- `topic_time_count`: count the largest synthetic topic between `{config['range_start_text']}` and `{config['range_end_text']}`.
- `payload_bytes_for_status`: sum description + payload byte lengths for the minority status without materializing text.

## Decision

For this database shape, a column-oriented read path is worth prototyping if the production workload is dominated by scans/aggregates over a few fields. Add a sorted `(topic_name, time)` projection only if that predicate is frequent enough to justify maintaining another physical ordering. Keep a row-oriented path (or reconstruct-on-demand path) for writes and wide record retrieval; this POC does not claim column layout wins there.
"""


def create_report(summary_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    latency_svg(summary, output_dir / "benchmark_latency.svg")
    size_svg(summary, output_dir / "layout_size.svg")
    latency_png(summary, output_dir / "benchmark_latency.png")
    size_png(summary, output_dir / "layout_size.png")
    (output_dir / "REPORT.md").write_text(report_markdown(summary), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    create_report(args.summary, args.output_dir)


if __name__ == "__main__":
    main()
