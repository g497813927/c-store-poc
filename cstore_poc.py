#!/usr/bin/env python3
"""Privacy-preserving C-Store physical-layout POC for SQLite uploads.

An uploaded SQLite database is used only to derive aggregate shape statistics.
Synthetic rows are then generated into a neutral gzip-compressed CSV snapshot.
All layout construction and every timed query run on that mock snapshot.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import gzip
import hashlib
import json
import mmap
import os
import random
import shutil
import sqlite3
import statistics
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Sequence

import numpy as np


SCRIPT_VERSION = "1.1"
DEFAULT_WORK = Path("work/cstore_poc")
DEFAULT_RESULTS = Path("outputs")
DEFAULT_RANDOM_SEED = 20260923

CSV_COLUMNS = ("id", "uid", "topic_name", "time", "status", "description", "data")
SORT_KEY = ("topic_name", "time", "id")
ROW_HEADER = struct.Struct("<QQqihII")
UINT64 = np.dtype("<u8")
INT64 = np.dtype("<i8")
INT32 = np.dtype("<i4")
INT16 = np.dtype("<i2")


@dataclass(frozen=True)
class Record:
    dynamic_id: int
    uid: int
    topic_code: int
    epoch_seconds: int
    status: int
    description: bytes
    data: bytes


class CountingSink:
    """A file-like sink that counts compressed bytes without retaining them."""

    def __init__(self) -> None:
        self.size = 0

    def write(self, payload: bytes) -> int:
        self.size += len(payload)
        return len(payload)

    def flush(self) -> None:
        return None


def stable_json_dump(payload: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_time(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return calendar.timegm(parsed.timetuple())


def format_time(epoch_seconds: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch_seconds))


def distribution_summary(values: Sequence[int]) -> dict[str, float | int]:
    ordered = sorted(values)
    if not ordered:
        return {key: 0 for key in ("min", "p25", "median", "p75", "p90", "p99", "max", "mean")}
    return {
        "min": ordered[0],
        "p25": quantile(ordered, 0.25),
        "median": quantile(ordered, 0.50),
        "p75": quantile(ordered, 0.75),
        "p90": quantile(ordered, 0.90),
        "p99": quantile(ordered, 0.99),
        "max": ordered[-1],
        "mean": statistics.fmean(ordered),
    }


def profile_database(db_path: Path) -> dict[str, object]:
    """Read only aggregate shape; never retain source rows, labels, or payloads."""
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only = ON")
    required = set(CSV_COLUMNS)
    try:
        schema_rows = connection.execute("PRAGMA table_info(dynamics)").fetchall()
        schema = [
            {"name": row[1], "type": row[2], "not_null": bool(row[3]), "primary_key": bool(row[5])}
            for row in schema_rows
        ]
        available = {column["name"] for column in schema}
        missing = sorted(required - available)
        if missing:
            raise ValueError(f"The uploaded database needs a dynamics table with columns {sorted(required)}; missing {missing}")

        topic_counts: Counter[str] = Counter()
        status_counts: Counter[int] = Counter()
        distinct_uids: set[int] = set()
        timestamps: list[int] = []
        description_lengths: list[int] = []
        data_lengths: list[int] = []
        null_rows = 0
        query = (
            "SELECT uid, topic_name, time, status, "
            "length(CAST(description AS BLOB)), length(CAST(data AS BLOB)) "
            "FROM dynamics"
        )
        for uid, topic, timestamp, status, description_length, data_length in connection.execute(query):
            if None in (uid, topic, timestamp, status, description_length, data_length):
                null_rows += 1
                continue
            distinct_uids.add(int(uid))
            topic_counts[str(topic)] += 1
            status_counts[int(status)] += 1
            timestamps.append(parse_time(str(timestamp)))
            description_lengths.append(int(description_length))
            data_lengths.append(int(data_length))
    finally:
        connection.close()

    if not timestamps:
        raise ValueError("No complete dynamics rows were available for profiling")
    return {
        "profile_version": 1,
        "privacy_mode": "aggregate shape only; no source path, hash, IDs, topic labels, descriptions, or payloads retained",
        "schema": schema,
        "complete_row_count": len(timestamps),
        "excluded_null_rows": null_rows,
        "distinct_uid_count": len(distinct_uids),
        "topic_frequencies_ranked": sorted(topic_counts.values(), reverse=True),
        "status_frequencies": {str(key): value for key, value in sorted(status_counts.items())},
        "min_time": format_time(min(timestamps)),
        "max_time": format_time(max(timestamps)),
        "description_length_bytes": distribution_summary(description_lengths),
        "data_length_bytes": distribution_summary(data_lengths),
        "mock_seed": DEFAULT_RANDOM_SEED,
    }


def scaled_frequencies(frequencies: Sequence[int], row_count: int) -> list[int]:
    total = sum(frequencies)
    if total <= 0:
        raise ValueError("Frequency total must be positive")
    raw = [value * row_count / total for value in frequencies]
    counts = [int(value) for value in raw]
    remainder = row_count - sum(counts)
    order = sorted(range(len(raw)), key=lambda index: raw[index] - counts[index], reverse=True)
    for index in order[:remainder]:
        counts[index] += 1
    return counts


def sample_length(summary: dict[str, float | int], rng: random.Random) -> int:
    anchors = [
        (0.00, int(summary["min"])),
        (0.25, int(summary["p25"])),
        (0.50, int(summary["median"])),
        (0.75, int(summary["p75"])),
        (0.90, int(summary["p90"])),
        (0.99, int(summary["p99"])),
        (1.00, int(summary["max"])),
    ]
    draw = rng.random()
    for (left_p, left_value), (right_p, right_value) in zip(anchors, anchors[1:]):
        if draw <= right_p:
            fraction = (draw - left_p) / max(right_p - left_p, 1e-12)
            return max(0, round(left_value + (right_value - left_value) * fraction))
    return int(summary["max"])


def mock_text(length: int, row_index: int, kind: str) -> str:
    if length <= 0:
        return ""
    prefix = f'{{"mock":true,"kind":"{kind}","row":{row_index},"value":"'
    suffix = '"}'
    if length <= len(prefix) + len(suffix):
        return ("m" * length)[:length]
    remaining = length - len(prefix) - len(suffix)
    tokens: list[str] = []
    token_index = 0
    built_length = 0
    while built_length < remaining:
        token = f"v{(row_index * 131 + token_index * 17) % 257:03d}_"
        tokens.append(token)
        built_length += len(token)
        token_index += 1
    body = "".join(tokens)[:remaining]
    return prefix + body + suffix


def generate_mock_extract(
    profile: dict[str, object],
    extract_dir: Path,
    row_limit: int | None = None,
) -> Path:
    """Generate a synthetic neutral snapshot; no source row values are copied."""
    prepare_directory(extract_dir)
    source_rows = int(profile["complete_row_count"])
    row_count = min(source_rows, row_limit) if row_limit else source_rows
    if row_count <= 0:
        raise ValueError("Mock row count must be positive")
    rng = random.Random(int(profile["mock_seed"]))
    topic_counts = scaled_frequencies(
        [int(value) for value in profile["topic_frequencies_ranked"]], row_count
    )
    topic_names = [f"topic_{index:03d}" for index in range(len(topic_counts))]
    topic_values = [topic for topic, count in zip(topic_names, topic_counts) for _ in range(count)]
    rng.shuffle(topic_values)

    status_items = sorted((int(key), int(value)) for key, value in profile["status_frequencies"].items())
    status_counts = scaled_frequencies([count for _, count in status_items], row_count)
    status_values = [status for (status, _), count in zip(status_items, status_counts) for _ in range(count)]
    rng.shuffle(status_values)

    min_time = parse_time(str(profile["min_time"]))
    max_time = parse_time(str(profile["max_time"]))
    distinct_uid_count = max(1, min(int(profile["distinct_uid_count"]), row_count))
    csv_path = extract_dir / "mock_dynamics.csv.gz"
    temp_path = extract_dir / "mock_dynamics.csv.gz.partial"
    with gzip.open(temp_path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for index in range(row_count):
            epoch_seconds = rng.randint(min_time, max_time)
            description_length = sample_length(profile["description_length_bytes"], rng)
            data_length = sample_length(profile["data_length_bytes"], rng)
            writer.writerow(
                (
                    1_000_000_000_000 + index,
                    1 + ((index * 104729) % distinct_uid_count),
                    topic_values[index],
                    format_time(epoch_seconds),
                    status_values[index],
                    mock_text(description_length, index, "description"),
                    mock_text(data_length, index, "payload"),
                )
            )
    temp_path.replace(csv_path)
    manifest = {
        "script_version": SCRIPT_VERSION,
        "privacy": "synthetic data only; no source row values copied",
        "row_count": row_count,
        "columns": list(CSV_COLUMNS),
        "mock_seed": int(profile["mock_seed"]),
        "mock_extract_bytes": csv_path.stat().st_size,
        "mock_extract_sha256": sha256_file(csv_path),
        "sqlite_role": "aggregate profiling only; never used by layout construction or timed queries",
    }
    stable_json_dump(manifest, extract_dir / "mock_manifest.json")
    print(f"Generated {row_count:,} synthetic rows at {csv_path}")
    return csv_path


def random_profile(row_count: int, topic_count: int, seed: int) -> dict[str, object]:
    """Create a standalone profile that does not depend on an uploaded database."""
    if row_count <= 0:
        raise ValueError("Random row count must be positive")
    if not 1 <= topic_count <= min(row_count, 256):
        raise ValueError("Topic count must be between 1 and min(row_count, 256)")
    weights = [1 / (rank**0.9) for rank in range(1, topic_count + 1)]
    scale = 1_000_000
    topic_frequencies = scaled_frequencies(
        [max(1, round(weight * scale)) for weight in weights],
        row_count,
    )
    minority = max(1, round(row_count * 0.12))
    return {
        "profile_version": 1,
        "privacy_mode": "fully random standalone profile; no uploaded database used",
        "schema": [
            {"name": "id", "type": "INTEGER", "not_null": True, "primary_key": True},
            {"name": "uid", "type": "INTEGER", "not_null": True, "primary_key": False},
            {"name": "topic_name", "type": "TEXT", "not_null": True, "primary_key": False},
            {"name": "time", "type": "TEXT", "not_null": True, "primary_key": False},
            {"name": "status", "type": "INTEGER", "not_null": True, "primary_key": False},
            {"name": "description", "type": "TEXT", "not_null": True, "primary_key": False},
            {"name": "data", "type": "TEXT", "not_null": True, "primary_key": False},
        ],
        "complete_row_count": row_count,
        "excluded_null_rows": 0,
        "distinct_uid_count": max(1, round(row_count * 0.72)),
        "topic_frequencies_ranked": topic_frequencies,
        "status_frequencies": {"0": row_count - minority, "1": minority},
        "min_time": "2021-01-01 00:00:00",
        "max_time": "2025-12-31 23:59:59",
        "description_length_bytes": {
            "min": 0, "p25": 48, "median": 96, "p75": 160,
            "p90": 260, "p99": 640, "max": 3_000, "mean": 132.0,
        },
        "data_length_bytes": {
            "min": 1_000, "p25": 2_800, "median": 3_300, "p75": 3_900,
            "p90": 4_500, "p99": 6_000, "max": 24_000, "mean": 3_450.0,
        },
        "mock_seed": seed,
    }


def iter_extract(csv_path: Path) -> Iterator[tuple[str, ...]]:
    with gzip.open(csv_path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = tuple(next(reader))
        if header != CSV_COLUMNS:
            raise ValueError(f"Unexpected extract columns: {header}")
        for row in reader:
            if len(row) != len(CSV_COLUMNS):
                raise ValueError(f"Malformed extract row with {len(row)} fields")
            yield tuple(row)


def load_records(csv_path: Path) -> tuple[list[Record], list[str], dict[str, object]]:
    raw_rows = list(iter_extract(csv_path))
    topics = sorted({row[2] for row in raw_rows})
    topic_codes = {topic: index for index, topic in enumerate(topics)}
    records: list[Record] = []
    status_counts: Counter[int] = Counter()
    topic_counts: Counter[str] = Counter()
    description_bytes = 0
    data_bytes = 0
    timestamps: list[int] = []

    for row in raw_rows:
        epoch_seconds = parse_time(row[3])
        description = row[5].encode("utf-8")
        data = row[6].encode("utf-8")
        record = Record(
            dynamic_id=int(row[0]),
            uid=int(row[1]),
            topic_code=topic_codes[row[2]],
            epoch_seconds=epoch_seconds,
            status=int(row[4]),
            description=description,
            data=data,
        )
        records.append(record)
        status_counts[record.status] += 1
        topic_counts[row[2]] += 1
        description_bytes += len(description)
        data_bytes += len(data)
        timestamps.append(epoch_seconds)

    ids = [record.dynamic_id for record in records]
    profile = {
        "row_count": len(records),
        "column_count": len(CSV_COLUMNS),
        "distinct_ids": len(set(ids)),
        "distinct_uids": len({record.uid for record in records}),
        "distinct_topics": len(topics),
        "status_counts": {str(key): value for key, value in sorted(status_counts.items())},
        "topic_counts": dict(topic_counts.most_common()),
        "min_time": format_time(min(timestamps)),
        "max_time": format_time(max(timestamps)),
        "description_utf8_bytes": description_bytes,
        "data_utf8_bytes": data_bytes,
        "exact_duplicate_ids": len(ids) - len(set(ids)),
        "nulls": 0,
        "notes": [
            "All rows are deterministic synthetic records generated from aggregate shape statistics.",
            "No source IDs, topic labels, descriptions, or payload values are present.",
            "Times are interpreted as UTC only to obtain a stable sortable integer; no timezone-dependent query is used.",
        ],
    }
    return records, topics, profile


def prepare_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def write_row_layout(records: Sequence[Record], topics: Sequence[str], path: Path, sorted_layout: bool) -> None:
    prepare_directory(path)
    offsets = np.empty(len(records) + 1, dtype=UINT64)
    row_path = path / "rows.bin"
    with row_path.open("wb") as handle:
        for index, record in enumerate(records):
            offsets[index] = handle.tell()
            handle.write(
                ROW_HEADER.pack(
                    record.dynamic_id,
                    record.uid,
                    record.epoch_seconds,
                    record.status,
                    record.topic_code,
                    len(record.description),
                    len(record.data),
                )
            )
            handle.write(record.description)
            handle.write(record.data)
        offsets[len(records)] = handle.tell()
    offsets.tofile(path / "row_offsets.u64")
    stable_json_dump(list(topics), path / "topics.json")
    stable_json_dump(
        {
            "format": "custom packed row store",
            "row_count": len(records),
            "row_header": "<QQqihII",
            "row_header_bytes": ROW_HEADER.size,
            "sort_key": list(SORT_KEY) if sorted_layout else [],
            "columns": list(CSV_COLUMNS),
        },
        path / "metadata.json",
    )


def write_bytes_column(values: Iterable[bytes], stem: str, path: Path, row_count: int) -> None:
    offsets = np.empty(row_count + 1, dtype=UINT64)
    with (path / f"{stem}.data").open("wb") as handle:
        for index, value in enumerate(values):
            offsets[index] = handle.tell()
            handle.write(value)
        offsets[row_count] = handle.tell()
    offsets.tofile(path / f"{stem}.offsets.u64")


def write_column_layout(records: Sequence[Record], topics: Sequence[str], path: Path, sorted_layout: bool) -> None:
    prepare_directory(path)
    np.fromiter((record.dynamic_id for record in records), dtype=UINT64, count=len(records)).tofile(path / "id.u64")
    np.fromiter((record.uid for record in records), dtype=UINT64, count=len(records)).tofile(path / "uid.u64")
    np.fromiter((record.epoch_seconds for record in records), dtype=INT64, count=len(records)).tofile(path / "time.i64")
    np.fromiter((record.status for record in records), dtype=INT32, count=len(records)).tofile(path / "status.i32")
    np.fromiter((record.topic_code for record in records), dtype=INT16, count=len(records)).tofile(path / "topic.i16")
    write_bytes_column((record.description for record in records), "description", path, len(records))
    write_bytes_column((record.data for record in records), "data", path, len(records))
    stable_json_dump(list(topics), path / "topics.json")
    stable_json_dump(
        {
            "format": "custom decomposed column store",
            "row_count": len(records),
            "sort_key": list(SORT_KEY) if sorted_layout else [],
            "columns": list(CSV_COLUMNS),
        },
        path / "metadata.json",
    )


def build_layouts(csv_path: Path, work_dir: Path) -> dict[str, object]:
    records, topics, profile = load_records(csv_path)
    stable_json_dump(profile, work_dir / "data_profile.json")
    layouts_dir = work_dir / "layouts"
    layouts_dir.mkdir(parents=True, exist_ok=True)

    write_row_layout(records, topics, layouts_dir / "row_unsorted", sorted_layout=False)
    write_column_layout(records, topics, layouts_dir / "column_unsorted", sorted_layout=False)

    records.sort(key=lambda record: (record.topic_code, record.epoch_seconds, record.dynamic_id))
    write_row_layout(records, topics, layouts_dir / "row_sorted", sorted_layout=True)
    write_column_layout(records, topics, layouts_dir / "column_sorted", sorted_layout=True)
    print(f"Built four layouts under {layouts_dir}")
    return profile


def gzip_size(path: Path, compresslevel: int = 6) -> int:
    sink = CountingSink()
    with gzip.GzipFile(fileobj=sink, mode="wb", compresslevel=compresslevel, mtime=0) as output:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                output.write(chunk)
    return sink.size


def layout_size_rows(layouts_dir: Path) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for layout in ("row_unsorted", "row_sorted", "column_unsorted", "column_sorted"):
        path = layouts_dir / layout
        files = sorted(item for item in path.iterdir() if item.is_file())
        raw_bytes = sum(item.stat().st_size for item in files)
        compressed_bytes = sum(gzip_size(item) for item in files)
        results.append(
            {
                "layout": layout,
                "raw_bytes": raw_bytes,
                "gzip_bytes": compressed_bytes,
                "raw_mib": raw_bytes / 1024**2,
                "gzip_mib": compressed_bytes / 1024**2,
                "file_count": len(files),
            }
        )
    return results


def read_metadata(layout_dir: Path) -> dict[str, object]:
    return json.loads((layout_dir / "metadata.json").read_text(encoding="utf-8"))


def scan_row_status_count(layout_dir: Path, target_status: int) -> int:
    count = 0
    with (layout_dir / "rows.bin").open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as payload:
            position = 0
            end = len(payload)
            while position < end:
                _, _, _, status, _, description_len, data_len = ROW_HEADER.unpack_from(payload, position)
                count += status == target_status
                position += ROW_HEADER.size + description_len + data_len
    return count


def scan_row_topic_time_count(
    layout_dir: Path,
    target_topic: int,
    start_time: int,
    end_time: int,
) -> int:
    count = 0
    with (layout_dir / "rows.bin").open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as payload:
            position = 0
            payload_end = len(payload)
            while position < payload_end:
                _, _, epoch_seconds, _, topic_code, description_len, data_len = ROW_HEADER.unpack_from(payload, position)
                count += topic_code == target_topic and start_time <= epoch_seconds <= end_time
                position += ROW_HEADER.size + description_len + data_len
    return count


def lower_bound_row(
    payload: mmap.mmap,
    offsets: np.memmap,
    target: tuple[int, int],
) -> int:
    low = 0
    high = len(offsets) - 1
    while low < high:
        middle = (low + high) // 2
        position = int(offsets[middle])
        _, _, epoch_seconds, _, topic_code, _, _ = ROW_HEADER.unpack_from(payload, position)
        if (topic_code, epoch_seconds) < target:
            low = middle + 1
        else:
            high = middle
    return low


def sorted_row_topic_time_count(
    layout_dir: Path,
    target_topic: int,
    start_time: int,
    end_time: int,
) -> int:
    offsets = np.memmap(layout_dir / "row_offsets.u64", dtype=UINT64, mode="r")
    with (layout_dir / "rows.bin").open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as payload:
            lower = lower_bound_row(payload, offsets, (target_topic, start_time))
            upper = lower_bound_row(payload, offsets, (target_topic, end_time + 1))
    return upper - lower


def scan_row_payload_bytes(layout_dir: Path, target_status: int) -> int:
    total = 0
    with (layout_dir / "rows.bin").open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as payload:
            position = 0
            end = len(payload)
            while position < end:
                _, _, _, status, _, description_len, data_len = ROW_HEADER.unpack_from(payload, position)
                if status == target_status:
                    total += description_len + data_len
                position += ROW_HEADER.size + description_len + data_len
    return total


def column_status_count(layout_dir: Path, target_status: int) -> int:
    values = np.memmap(layout_dir / "status.i32", dtype=INT32, mode="r")
    return int(np.count_nonzero(values == target_status))


def column_topic_time_count(
    layout_dir: Path,
    target_topic: int,
    start_time: int,
    end_time: int,
    sorted_layout: bool,
) -> int:
    topics = np.memmap(layout_dir / "topic.i16", dtype=INT16, mode="r")
    times = np.memmap(layout_dir / "time.i64", dtype=INT64, mode="r")
    if not sorted_layout:
        return int(np.count_nonzero((topics == target_topic) & (times >= start_time) & (times <= end_time)))
    topic_start = int(np.searchsorted(topics, target_topic, side="left"))
    topic_end = int(np.searchsorted(topics, target_topic, side="right"))
    local_times = times[topic_start:topic_end]
    lower = int(np.searchsorted(local_times, start_time, side="left"))
    upper = int(np.searchsorted(local_times, end_time, side="right"))
    return upper - lower


def column_payload_bytes(layout_dir: Path, target_status: int) -> int:
    statuses = np.memmap(layout_dir / "status.i32", dtype=INT32, mode="r")
    description_offsets = np.memmap(layout_dir / "description.offsets.u64", dtype=UINT64, mode="r")
    data_offsets = np.memmap(layout_dir / "data.offsets.u64", dtype=UINT64, mode="r")
    mask = statuses == target_status
    description_lengths = np.diff(description_offsets)
    data_lengths = np.diff(data_offsets)
    return int(description_lengths[mask].sum(dtype=np.uint64) + data_lengths[mask].sum(dtype=np.uint64))


def benchmark_call(function, repeats: int = 9, warmups: int = 2) -> tuple[object, list[float]]:
    expected = None
    for _ in range(warmups):
        value = function()
        expected = value if expected is None else expected
        if value != expected:
            raise AssertionError("Non-deterministic warmup result")
    timings: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        value = function()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        if value != expected:
            raise AssertionError("Non-deterministic measured result")
        timings.append(elapsed_ms)
    return expected, timings


def quantile(sorted_values: Sequence[int], fraction: float) -> int:
    if not sorted_values:
        raise ValueError("Cannot calculate a quantile of an empty sequence")
    index = round((len(sorted_values) - 1) * fraction)
    return int(sorted_values[index])


def benchmark_config(csv_path: Path) -> dict[str, object]:
    records, topics, _ = load_records(csv_path)
    topic_counts = Counter(record.topic_code for record in records)
    target_topic, _ = topic_counts.most_common(1)[0]
    topic_times = sorted(
        record.epoch_seconds for record in records if record.topic_code == target_topic
    )
    status_counts = Counter(record.status for record in records)
    nonzero_statuses = [status for status, count in status_counts.items() if count > 0]
    target_status = min(nonzero_statuses, key=lambda status: (status_counts[status], status))
    return {
        "target_topic_code": target_topic,
        "target_topic_name": topics[target_topic],
        "target_topic_rows": topic_counts[target_topic],
        "range_start": quantile(topic_times, 0.25),
        "range_end": quantile(topic_times, 0.75),
        "range_start_text": format_time(quantile(topic_times, 0.25)),
        "range_end_text": format_time(quantile(topic_times, 0.75)),
        "target_status": target_status,
        "target_status_rows": status_counts[target_status],
        "repeats": 9,
        "warmups": 2,
        "cache_policy": "warm OS cache; each invocation reopens/memmaps its custom files",
    }


def logical_bytes_for_query(layout_dir: Path, layout: str, query: str, row_count: int) -> int:
    if layout.startswith("row"):
        if query == "topic_time_count" and layout in {"row_sorted", "column_sorted"}:
            probes = 4 * (row_count.bit_length() + 1)
            return probes * (8 + ROW_HEADER.size)
        return (layout_dir / "rows.bin").stat().st_size
    if query == "status_count":
        return (layout_dir / "status.i32").stat().st_size
    if query == "topic_time_count":
        if layout in {"row_sorted", "column_sorted"}:
            probes = 4 * (row_count.bit_length() + 1)
            return probes * (INT16.itemsize + INT64.itemsize)
        return (layout_dir / "topic.i16").stat().st_size + (layout_dir / "time.i64").stat().st_size
    if query == "payload_bytes_for_status":
        return sum(
            (layout_dir / name).stat().st_size
            for name in (
                "status.i32",
                "description.offsets.u64",
                "data.offsets.u64",
            )
        )
    raise ValueError(query)


def benchmark_layouts(csv_path: Path, work_dir: Path, results_dir: Path) -> dict[str, object]:
    results_dir.mkdir(parents=True, exist_ok=True)
    layouts_dir = work_dir / "layouts"
    profile = json.loads((work_dir / "data_profile.json").read_text(encoding="utf-8"))
    config = benchmark_config(csv_path)
    stable_json_dump(config, work_dir / "benchmark_config.json")

    benchmark_rows: list[dict[str, object]] = []
    expected_by_query: dict[str, object] = {}
    for layout in ("row_unsorted", "row_sorted", "column_unsorted", "column_sorted"):
        layout_dir = layouts_dir / layout
        is_sorted = layout in {"row_sorted", "column_sorted"}
        calls = {
            "status_count": lambda layout_dir=layout_dir: (
                scan_row_status_count(layout_dir, int(config["target_status"]))
                if layout.startswith("row")
                else column_status_count(layout_dir, int(config["target_status"]))
            ),
            "topic_time_count": lambda layout_dir=layout_dir, is_sorted=is_sorted: (
                (
                    sorted_row_topic_time_count(
                        layout_dir,
                        int(config["target_topic_code"]),
                        int(config["range_start"]),
                        int(config["range_end"]),
                    )
                    if is_sorted
                    else scan_row_topic_time_count(
                        layout_dir,
                        int(config["target_topic_code"]),
                        int(config["range_start"]),
                        int(config["range_end"]),
                    )
                )
                if layout.startswith("row")
                else column_topic_time_count(
                    layout_dir,
                    int(config["target_topic_code"]),
                    int(config["range_start"]),
                    int(config["range_end"]),
                    is_sorted,
                )
            ),
            "payload_bytes_for_status": lambda layout_dir=layout_dir: (
                scan_row_payload_bytes(layout_dir, int(config["target_status"]))
                if layout.startswith("row")
                else column_payload_bytes(layout_dir, int(config["target_status"]))
            ),
        }
        for query_name, call in calls.items():
            value, timings = benchmark_call(call)
            if query_name in expected_by_query and expected_by_query[query_name] != value:
                raise AssertionError(
                    f"Correctness mismatch for {query_name}: {layout} produced {value}; "
                    f"expected {expected_by_query[query_name]}"
                )
            expected_by_query[query_name] = value
            benchmark_rows.append(
                {
                    "layout": layout,
                    "query": query_name,
                    "result": value,
                    "median_ms": statistics.median(timings),
                    "mean_ms": statistics.fmean(timings),
                    "min_ms": min(timings),
                    "max_ms": max(timings),
                    "stdev_ms": statistics.stdev(timings),
                    "logical_bytes_accessed": logical_bytes_for_query(
                        layout_dir, layout, query_name, int(profile["row_count"])
                    ),
                    "repeats": len(timings),
                }
            )
            print(f"{layout:16s} {query_name:26s} median={statistics.median(timings):9.3f} ms")

    size_rows = layout_size_rows(layouts_dir)
    write_csv(results_dir / "benchmark_results.csv", benchmark_rows)
    write_csv(results_dir / "layout_sizes.csv", size_rows)
    stable_json_dump(profile, results_dir / "data_profile.json")
    stable_json_dump(config, results_dir / "benchmark_config.json")

    summary = {
        "script_version": SCRIPT_VERSION,
        "correctness": "PASS: every query returned the same result in all four layouts",
        "data_profile": profile,
        "benchmark_config": config,
        "benchmark_results": benchmark_rows,
        "layout_sizes": size_rows,
        "method": {
            "source": "deterministic synthetic CSV.gz generated from aggregate SQLite shape statistics",
            "timed_engine": "custom Python/NumPy row and column readers; no SQLite query is timed",
            "layouts": [
                "row_unsorted",
                "row_sorted by (topic_name, time, id)",
                "column_unsorted",
                "column_sorted by (topic_name, time, id)",
            ],
            "compression_metric": "actual gzip level-6 byte count, one stream per physical file",
            "timing_metric": "median of 9 warm-cache runs after 2 warmups",
        },
    }
    stable_json_dump(summary, results_dir / "cstore_poc_summary.json")
    return summary


def write_csv(path: Path, rows: Sequence[dict[str, object]]) -> None:
    if not rows:
        raise ValueError("Cannot write an empty CSV")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(rows[0].keys()),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def validate_outputs(work_dir: Path, results_dir: Path) -> None:
    required = [
        work_dir / "extract" / "mock_manifest.json",
        work_dir / "extract" / "mock_dynamics.csv.gz",
        work_dir / "data_profile.json",
        results_dir / "benchmark_results.csv",
        results_dir / "layout_sizes.csv",
        results_dir / "cstore_poc_summary.json",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing outputs: {missing}")
    summary = json.loads((results_dir / "cstore_poc_summary.json").read_text(encoding="utf-8"))
    if not str(summary.get("correctness", "")).startswith("PASS"):
        raise AssertionError("Correctness validation did not pass")
    if len(summary.get("benchmark_results", [])) != 12:
        raise AssertionError("Expected 12 benchmark result rows")
    if len(summary.get("layout_sizes", [])) != 4:
        raise AssertionError("Expected four layout size rows")
    print("Validation PASS: synthetic snapshot, four layouts, 12 query results, privacy mode, and cross-layout correctness")


def run_all(
    profile: dict[str, object],
    work_dir: Path,
    results_dir: Path,
    row_limit: int | None = None,
) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    stable_json_dump(profile, results_dir / "mock_profile.json")
    extract_path = generate_mock_extract(profile, work_dir / "extract", row_limit=row_limit)
    build_layouts(extract_path, work_dir)
    benchmark_layouts(extract_path, work_dir, results_dir)
    validate_outputs(work_dir, results_dir)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("profile", "mock", "build", "benchmark", "validate", "all"))
    parser.add_argument("--db", type=Path, help="SQLite upload used only for aggregate shape profiling")
    parser.add_argument("--profile", type=Path, help="Sanitized aggregate profile JSON; avoids opening SQLite")
    parser.add_argument("--random", action="store_true", help="Use a fully random standalone profile")
    parser.add_argument("--rows", type=int, default=25_000, help="Rows for --random (default: 25000)")
    parser.add_argument("--topics", type=int, default=16, help="Topics for --random (default: 16)")
    parser.add_argument("--seed", type=int, default=DEFAULT_RANDOM_SEED, help="Seed for --random")
    parser.add_argument("--row-limit", type=int, help="Optional synthetic row cap for quick UI/demo runs")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    extract_path = args.work_dir / "extract" / "mock_dynamics.csv.gz"
    if args.command == "profile":
        if not args.db:
            raise SystemExit("--db is required for the profile command")
        profile = profile_database(args.db)
        stable_json_dump(profile, args.results_dir / "mock_profile.json")
        print(f"Wrote sanitized aggregate profile to {args.results_dir / 'mock_profile.json'}")
    elif args.command == "mock":
        if not args.profile:
            raise SystemExit("--profile is required for the mock command")
        profile = json.loads(args.profile.read_text(encoding="utf-8"))
        generate_mock_extract(profile, args.work_dir / "extract", row_limit=args.row_limit)
    elif args.command == "build":
        build_layouts(extract_path, args.work_dir)
    elif args.command == "benchmark":
        benchmark_layouts(extract_path, args.work_dir, args.results_dir)
    elif args.command == "validate":
        validate_outputs(args.work_dir, args.results_dir)
    elif args.command == "all":
        source_count = sum((bool(args.db), bool(args.profile), bool(args.random)))
        if source_count != 1:
            raise SystemExit("For all, provide exactly one of --db, --profile, or --random")
        if args.db:
            profile = profile_database(args.db)
        elif args.profile:
            profile = json.loads(args.profile.read_text(encoding="utf-8"))
        else:
            profile = random_profile(args.rows, args.topics, args.seed)
        run_all(
            profile,
            args.work_dir,
            args.results_dir,
            row_limit=None if args.random else args.row_limit,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
