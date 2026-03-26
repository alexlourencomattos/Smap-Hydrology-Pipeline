#!/usr/bin/env python3
"""Extraction and preparation pipeline for SMAP ingestion.

Usage:
    python smap_ingestion_pipeline.py --config configs/smap_pipeline.example.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

import pandas as pd
import requests


REQUIRED_VARIABLES = {"precipitation", "streamflow", "etp"}


def read_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "For YAML configs, install PyYAML. "
                "Alternatively, use a JSON config file."
            ) from exc
        with path.open("r", encoding="utf-8") as file:
            return yaml.safe_load(file)

    if path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    raise ValueError("Invalid config format. Use .yaml, .yml, or .json")


def load_source_df(source: Dict[str, Any], raw_dir: Path) -> pd.DataFrame:
    source_name = source["name"]
    source_type = source.get("type", "local").lower()
    delimiter = source.get("delimiter", ",")
    encoding = source.get("encoding", "utf-8")

    if source_type == "url":
        url = source["url"]
        response = requests.get(url, timeout=120)
        response.raise_for_status()
        file_name = source.get("raw_filename", f"{source_name}.csv")
        raw_path = raw_dir / file_name
        raw_path.write_bytes(response.content)
    else:
        raw_path = Path(source["path"]).expanduser().resolve()
        if not raw_path.exists():
            raise FileNotFoundError(f"Local source file not found: {raw_path}")

    return pd.read_csv(raw_path, sep=delimiter, encoding=encoding, low_memory=False)


def apply_filters(df: pd.DataFrame, filters: Dict[str, Any]) -> pd.DataFrame:
    filtered = df.copy()
    for column, value in (filters or {}).items():
        if column not in filtered.columns:
            raise KeyError(f"Filter column '{column}' not found in dataset")

        if isinstance(value, list):
            filtered = filtered[filtered[column].isin(value)]
        else:
            filtered = filtered[filtered[column] == value]

    return filtered


def normalize_schema(df: pd.DataFrame, source: Dict[str, Any]) -> pd.DataFrame:
    mapping = source["schema"]

    required_fields = {"date", "value"}
    if not required_fields.issubset(mapping):
        raise KeyError(
            f"Schema for source '{source['name']}' must include at least: date and value"
        )

    reverse_map = {
        mapping["date"]: "date",
        mapping["value"]: "value",
    }

    if "station" in mapping:
        reverse_map[mapping["station"]] = "station"
    if "basin" in mapping:
        reverse_map[mapping["basin"]] = "basin"

    missing_cols = [column for column in reverse_map if column not in df.columns]
    if missing_cols:
        raise KeyError(
            f"Source '{source['name']}' missing expected columns after loading: {missing_cols}"
        )

    normalized = df[list(reverse_map.keys())].rename(columns=reverse_map)
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    normalized = normalized.dropna(subset=["date", "value"]).copy()

    normalized["value"] = pd.to_numeric(normalized["value"], errors="coerce")
    normalized = normalized.dropna(subset=["value"]).copy()

    normalized["variable"] = source["variable"].lower()
    normalized["source"] = source.get("origin", source["name"])
    normalized = normalized.sort_values("date").reset_index(drop=True)

    return normalized


def run_pipeline(config_path: Path) -> None:
    cfg = read_config(config_path)

    raw_dir = Path(cfg.get("raw_dir", "data/raw"))
    processed_dir = Path(cfg.get("processed_dir", "data/processed"))
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    all_frames = []
    variables_found = set()

    for source in cfg.get("sources", []):
        df = load_source_df(source, raw_dir)
        df = apply_filters(df, source.get("filters", {}))
        normalized = normalize_schema(df, source)

        variables_found.add(source["variable"].lower())

        out_name = source.get("output_name", f"{source['variable'].lower()}.csv")
        normalized.to_csv(processed_dir / out_name, index=False)
        all_frames.append(normalized)

        print(
            f"[OK] {source['name']}: {len(normalized)} records exported to "
            f"{processed_dir / out_name}"
        )

    missing_vars = REQUIRED_VARIABLES - variables_found
    if missing_vars:
        print(
            "[WARNING] Missing minimum variables for full SMAP ingestion: "
            f"{sorted(missing_vars)}"
        )

    if not all_frames:
        raise RuntimeError("No sources were processed. Check your configuration.")

    merged = pd.concat(all_frames, ignore_index=True)
    merged.to_csv(processed_dir / "smap_ingestion_unified.csv", index=False)
    print(f"[OK] Unified dataset saved to {processed_dir / 'smap_ingestion_unified.csv'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SMAP ingestion pipeline")
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Path to .yaml/.yml/.json configuration file",
    )
    arguments = parser.parse_args()
    run_pipeline(arguments.config)
