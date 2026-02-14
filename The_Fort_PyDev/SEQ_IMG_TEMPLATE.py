"""
SEQ_IMG_TEMPLATE.py

Template application for reading sequenced aerial image metadata produced by SequenceAerialImages.py.

What it does
- Reads all inputs from a key/value pair file (SEQ_IMG_TEMPLATE.kvp)
- Loads the aerial log CSV and *stores* metadata for all images in memory (keyed by image index)
- Loops from first to last index, retrieves metadata for each index from the in-memory store, and prints it

Notes
- This script is intentionally lightweight and easy to copy/rename/modify.
- The log file referenced here is expected to be the provider reference CSV you maintain as
  your authoritative metadata file (e.g., AerialImages_referenceinfo_121425_converted.csv).
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional, Iterable, Tuple


# -----------------------------
# KVP parsing
# -----------------------------

def read_kvp(path: Path) -> Dict[str, str]:
    """
    Read a simple key=value configuration file.

    Supported:
    - Comments starting with '#' or ';'
    - Blank lines
    - Values may be quoted (single or double)
    """
    if not path.exists():
        raise FileNotFoundError(f"KVP file not found: {path}")

    cfg: Dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid KVP line {lineno} (missing '='): {raw}")
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            raise ValueError(f"Invalid KVP line {lineno} (empty key): {raw}")
        # Strip quotes if present
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        cfg[key] = val
    return cfg


# -----------------------------
# Metadata store
# -----------------------------

@dataclass(frozen=True)
class ImageMeta:
    """Holds one image's metadata as parsed from the aerial log file."""
    idx: int
    fields: Dict[str, Any]

    def __getitem__(self, item: str) -> Any:
        return self.fields.get(item)


class ImageMetaStore:
    """
    Queryable metadata store keyed by integer image index.

    This class loads the CSV once and stores all metadata rows in memory.

    Usage:
        store = ImageMetaStore.from_csv(csv_path)
        meta = store.get(12)  # ImageMeta for index 12 (or None if missing)
    """

    def __init__(self, by_idx: Dict[int, ImageMeta]) -> None:
        self._by_idx = dict(by_idx)

    @staticmethod
    def _detect_idx_field(fieldnames: Iterable[str]) -> str:
        # Common column names we might see
        candidates = ("idx", "index", "image_index", "id", "Idx", "Index", "IMAGE_INDEX")
        fset = set(fieldnames)
        for c in candidates:
            if c in fset:
                return c
        # Fall back: try anything that looks like "idx" ignoring case
        for f in fieldnames:
            if f.lower() == "idx":
                return f
        raise ValueError(f"Could not find an index column in CSV header. Found: {list(fieldnames)}")

    @classmethod
    def from_csv(cls, csv_path: Path) -> "ImageMetaStore":
        """
        Load the CSV log file and store metadata for each image in memory, keyed by idx.
        """
        if not csv_path.exists():
            raise FileNotFoundError(f"Aerial log CSV not found: {csv_path}")

        by_idx: Dict[int, ImageMeta] = {}
        with csv_path.open("r", newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise ValueError(f"CSV appears to have no header: {csv_path}")
            idx_field = cls._detect_idx_field(reader.fieldnames)

            for row_no, row in enumerate(reader, start=2):  # header is line 1
                raw_idx = (row.get(idx_field) or "").strip()
                if not raw_idx:
                    # Skip blank lines
                    continue
                try:
                    idx = int(float(raw_idx))  # tolerate "12.0" etc.
                except ValueError as e:
                    raise ValueError(f"Invalid index '{raw_idx}' at CSV line {row_no}") from e

                # Keep the full row, but remove the idx field from "fields"
                fields = dict(row)
                fields.pop(idx_field, None)

                by_idx[idx] = ImageMeta(idx=idx, fields=fields)

        if not by_idx:
            raise ValueError(f"No metadata rows were loaded from: {csv_path}")

        return cls(by_idx)

    def get(self, idx: int) -> Optional[ImageMeta]:
        return self._by_idx.get(idx)

    def index_range(self) -> Tuple[int, int]:
        keys = sorted(self._by_idx.keys())
        return keys[0], keys[-1]

    def to_fields_dict(self) -> Dict[int, Dict[str, Any]]:
        """
        Return a plain dictionary mapping idx -> fields.

        This makes it explicit that metadata is stored in memory before any looping/processing.
        """
        return {idx: meta.fields for idx, meta in self._by_idx.items()}

    def __len__(self) -> int:
        return len(self._by_idx)


# -----------------------------
# Main
# -----------------------------

def main(argv: list[str]) -> int:
    # Determine kvp path: CLI arg or default alongside this script
    if len(argv) > 2:
        print("Usage: python SEQ_IMG_TEMPLATE.py [path/to/SEQ_IMG_TEMPLATE.kvp]", file=sys.stderr)
        return 2

    script_dir = Path(__file__).resolve().parent
    kvp_path = Path(argv[1]) if len(argv) == 2 else (script_dir / "SEQ_IMG_TEMPLATE.kvp")

    cfg = read_kvp(kvp_path)

    # Required keys
    required = ("aerial_log_file", "sequences_path")
    missing = [k for k in required if k not in cfg]
    if missing:
        raise KeyError(f"Missing required KVP keys: {missing}. Present keys: {sorted(cfg.keys())}")

    aerial_log_file = Path(cfg["aerial_log_file"])
    sequences_path = Path(cfg["sequences_path"])

    # 1) Read CSV log file and store all metadata in memory BEFORE looping.
    store = ImageMetaStore.from_csv(aerial_log_file)
    meta_by_idx = store.to_fields_dict()  # idx -> metadata fields (in-memory)

    # Report what we loaded
    min_idx, max_idx = store.index_range()
    print(f"Loaded {len(store)} image metadata rows from: {aerial_log_file}")
    print(f"Index range: {min_idx} .. {max_idx}")
    print(f"Sequences path (for your future use): {sequences_path}")
    print("-" * 80)

    # 2) Loop first to last index, retrieving metadata from the in-memory dict
    for idx in range(min_idx, max_idx + 1):
        fields = meta_by_idx.get(idx)
        if fields is None:
            print(f"idx={idx:04d}  [MISSING METADATA ROW]")
            continue

        # Pretty-print a few common fields if present, else print the whole dict
        filename = fields.get("filename") or fields.get("Label") or fields.get("label")
        lon = fields.get("lon") or fields.get("X/Longitude") or fields.get("Longitude")
        lat = fields.get("lat") or fields.get("Y/Latitude") or fields.get("Latitude")
        alt = fields.get("alt") or fields.get("Z/Altitude") or fields.get("Altitude")

        if any(v is not None for v in (filename, lon, lat, alt)):
            print(f"idx={idx:04d}  file={filename}  lon={lon}  lat={lat}  alt={alt}")
        else:
            print(f"idx={idx:04d}  fields={fields}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
