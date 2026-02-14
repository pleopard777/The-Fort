"""
SequenceAerialImages.py  (V005 FINAL)

What changed in V004
--------------------
V004 removes ALL GUI code (no Tkinter, no dialogs). Every required input is now provided
via a simple text configuration file named:

    SequenceAerialImages.kvp

That KVP file MUST reside in the same folder as this script.

At runtime, this script:
  1) Locates its own folder (the "script directory")
  2) Loads SequenceAerialImages.kvp from that folder
  3) Executes the sequencing + metadata workflow
  4) Prints a run summary to stdout (console / Spyder / terminal)

Key idea
--------
You can move this script anywhere, as long as you move its KVP file with it.

KVP file format (SequenceAerialImages.kvp)
------------------------------------------
- One setting per line:

      key = value

- Whitespace around key/value is ignored.
- Blank lines are ignored.
- Comment lines begin with "#".
- Inline comments are allowed if they start with " #"
  (space + hash), e.g.:
      source_dir = "C:\data\images" # folder of input images

- Values may be quoted with single or double quotes.

Required keys:
  - source_dir
  - composite_tif

Optional keys:
  - output_subdir         (default: Sequenced)
  - overwrite_existing    (default: false)
  - recurse_source        (default: false)
  - force_output_ext      (default: .jpg)

What "sequencing" means
-----------------------
This tool looks for aerial images in source_dir whose *stem* (filename without extension)
matches this pattern:

    RockSpringRd_<DATE>_Aerial (<ID>)

Examples (stem only):
    RockSpringRd_120925_Aerial (1)
    RockSpringRd_120925_Aerial (31)
    RockSpringRd_120925_Aerial (192)

Where:
  - <DATE> is exactly 6 digits (MMDDYY)
  - <ID> is an integer (the number inside parentheses)

For each matching file, the tool generates a new destination filename:

    <NUM>_RockSpringRd_<DATE>_Aerial.jpg

Where:
  - <NUM> is <ID> zero-padded to 5 digits (e.g., 00001, 00192)
  - Destination extension is controlled by force_output_ext (default ".jpg")

IMPORTANT (no image conversion)
-------------------------------
The destination extension is only a naming convention. The tool copies the file bytes
as-is and does NOT convert formats.

So a file like:
    RockSpringRd_120925_Aerial (7).png
may be copied to:
    00007_RockSpringRd_120925_Aerial.jpg

If you want real conversion, we can add that in a future version.

Output folder behavior
----------------------
Inside source_dir, the script creates (or reuses):

    <source_dir>\<output_subdir>

Default output_subdir is "Sequenced".

Idempotency:
  - If overwrite_existing=false (recommended), the script will NOT copy a file if the
    destination filename already exists. The file is still included in the metadata CSV.
  - If overwrite_existing=true, the destination file will be overwritten by copy2().

Dependencies
------------
- Python 3.x
- Pillow (PIL)
- GDAL (osgeo.gdal, osgeo.osr)
- NumPy (required for scanning the alpha/mask to compute AOI area and bbox)

"""

from __future__ import annotations

import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple, Optional

from PIL import Image
from osgeo import gdal, osr


# =============================================================================
# File filters / naming rules
# =============================================================================

# Image extensions considered "images" for this workflow.
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif"
}

# Aerial image naming convention (applied to the file stem)
AERIAL_NAME_RE = re.compile(r"^RockSpringRd_(\d{6})_Aerial \((\d+)\)$")

# Unit conversion constants
M2_PER_ACRE = 4046.8564224
FT_PER_M = 3.280839895


# =============================================================================
# Configuration handling
# =============================================================================

@dataclass(frozen=True)
class Config:
    """
    Runtime configuration loaded from SequenceAerialImages.kvp.
    """
    source_dir: Path
    composite_tif: Path
    output_subdir: str = "Sequenced"
    overwrite_existing: bool = False
    recurse_source: bool = False
    force_output_ext: str = ".jpg"


def _strip_optional_quotes(s: str) -> str:
    """
    Remove surrounding single or double quotes if present.

    Examples:
        '"C:\\Data"' -> 'C:\\Data'
        "'hello'"    -> 'hello'
        'hello'      -> 'hello'
    """
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def read_kvp(kvp_path: Path) -> Dict[str, str]:
    """
    Read a key/value pair file.

    Accepted line formats:
      - key = value
      - # comment
      - blank line

    Inline comments are supported ONLY when written as " #...".
    (Space + #) prevents accidental truncation of rare paths containing '#'.

    Returns:
        dict of lower-cased keys to string values
    """
    if not kvp_path.exists():
        raise FileNotFoundError(f"Missing config file: {kvp_path}")

    data: Dict[str, str] = {}
    for lineno, raw in enumerate(kvp_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue

        # Optional inline comment support
        if " #" in line:
            line = line.split(" #", 1)[0].rstrip()

        if "=" not in line:
            raise ValueError(f"{kvp_path.name}:{lineno}: expected 'key = value' but got: {raw!r}")

        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_optional_quotes(value.strip())
        if not key:
            raise ValueError(f"{kvp_path.name}:{lineno}: empty key is not allowed.")

        data[key.lower()] = value

    return data


def _parse_bool(s: str) -> bool:
    """
    Parse common boolean forms.

    True:
      1, true, yes, y, on
    False:
      0, false, no, n, off
    """
    v = s.strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    raise ValueError(f"Invalid boolean value: {s!r} (use true/false, yes/no, 1/0)")


def load_config(script_dir: Path) -> Config:
    """
    Load configuration from SequenceAerialImages.kvp located next to this script.

    Required keys:
      - source_dir
      - composite_tif

    Optional keys:
      - output_subdir
      - overwrite_existing
      - recurse_source
      - force_output_ext

    Returns:
        Config dataclass
    """
    kvp_path = script_dir / "SequenceAerialImages.kvp"
    kvp = read_kvp(kvp_path)

    def req(key: str) -> str:
        if key not in kvp or not kvp[key].strip():
            raise ValueError(f"Missing required key '{key}' in {kvp_path}")
        return kvp[key].strip()

    source_dir = Path(req("source_dir")).expanduser()
    composite_tif = Path(req("composite_tif")).expanduser()

    output_subdir = kvp.get("output_subdir", "Sequenced").strip() or "Sequenced"

    force_output_ext = kvp.get("force_output_ext", ".jpg").strip() or ".jpg"
    if not force_output_ext.startswith("."):
        force_output_ext = "." + force_output_ext

    overwrite_existing = _parse_bool(kvp.get("overwrite_existing", "false"))
    recurse_source = _parse_bool(kvp.get("recurse_source", "false"))

    return Config(
        source_dir=source_dir,
        composite_tif=composite_tif,
        output_subdir=output_subdir,
        metadata_csv_name=metadata_csv_name,
        overwrite_existing=overwrite_existing,
        recurse_source=recurse_source,
        force_output_ext=force_output_ext,
    )


# =============================================================================
# Image / AOI helpers
# =============================================================================

def get_basic_image_props(path: Path) -> Tuple[Optional[int], Optional[int], int]:
    """
    Extract per-image properties:
      - width in pixels
      - height in pixels
      - file size in bytes

    Width/height is a best-effort read using Pillow.
    """
    size_bytes = path.stat().st_size
    width = height = None
    try:
        with Image.open(path) as im:
            width, height = im.size
    except Exception:
        pass
    return width, height, size_bytes


def _pixel_corner_to_map(gt: Tuple[float, float, float, float, float, float],
                         col: float, row: float) -> Tuple[float, float]:
    """
    Pixel CORNER (col,row) -> map (x,y) using GDAL geotransform.
    """
    x = gt[0] + col * gt[1] + row * gt[2]
    y = gt[3] + col * gt[4] + row * gt[5]
    return x, y


def _pixel_center_to_map(gt: Tuple[float, float, float, float, float, float],
                         col: float, row: float) -> Tuple[float, float]:
    """
    Pixel CENTER (col,row) -> map (x,y) using GDAL geotransform.
    """
    x = gt[0] + (col + 0.5) * gt[1] + (row + 0.5) * gt[2]
    y = gt[3] + (col + 0.5) * gt[4] + (row + 0.5) * gt[5]
    return x, y


def compute_aoi_info_from_composite(composite_tif: Path) -> Dict:
    """
    Compute AOI metadata from a composite ortho GeoTIFF using its valid-data footprint.

    Strategy:
      - Prefer alpha band if present (mask/alpha > 0 indicates valid imagery)
      - Otherwise use GDAL mask band (often derived from NoData)

    Returns:
      dict with:
        corners: ul/ur/lr/ll -> (lat, lon)
        compass: nwc/nec/sec/swc -> (lat, lon)
        centroid: (lat, lon)
        area_acres: float
        px_x_units, px_y_units: float
        px_x_ft, px_y_ft: float
    """
    if not composite_tif.exists():
        raise FileNotFoundError(f"Composite GeoTIFF not found: {composite_tif}")

    gdal.SetConfigOption("GDAL_READ_WORLDFILE", "YES")
    ds = gdal.Open(str(composite_tif), gdal.GA_ReadOnly)
    if not ds:
        raise RuntimeError(f"GDAL could not open composite: {composite_tif}")

    try:
        gt = ds.GetGeoTransform(can_return_null=True)
        if gt is None:
            raise RuntimeError("Composite has no geotransform (no internal georef and world file not applied).")

        proj_wkt = (ds.GetProjection() or "").strip()
        if not proj_wkt:
            prj_path = composite_tif.with_suffix(".prj")
            if prj_path.exists():
                proj_wkt = prj_path.read_text(encoding="utf-8").strip()
        if not proj_wkt:
            raise RuntimeError("Composite has no projection (and no .prj found); cannot transform to lat/lon.")

        # Force traditional GIS axis order to prevent lat/lon swaps
        src = osr.SpatialReference()
        src.ImportFromWkt(proj_wkt)
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        meters_per_unit = float(src.GetLinearUnits() or 1.0)

        dst = osr.SpatialReference()
        dst.ImportFromEPSG(4326)
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tx = osr.CoordinateTransformation(src, dst)

        w = ds.RasterXSize
        h = ds.RasterYSize

        # Choose alpha band if present; else mask band.
        alpha_band = None
        for i in range(1, ds.RasterCount + 1):
            b = ds.GetRasterBand(i)
            if b.GetColorInterpretation() == gdal.GCI_AlphaBand:
                alpha_band = b
                break
        mask_band = alpha_band if alpha_band is not None else ds.GetRasterBand(1).GetMaskBand()

        # NumPy required for scanning valid pixels efficiently
        try:
            import numpy as np
        except Exception as e:
            raise RuntimeError("NumPy is required for AOI mask scanning. Install numpy and retry.") from e

        block_x, block_y = mask_band.GetBlockSize()
        if block_x <= 0 or block_y <= 0:
            block_x, block_y = 1024, 1024

        min_c, max_c = w, -1
        min_r, max_r = h, -1

        valid_count = 0
        sum_c = 0.0
        sum_r = 0.0

        # Scan mask blocks
        for yoff in range(0, h, block_y):
            ysize = min(block_y, h - yoff)
            for xoff in range(0, w, block_x):
                xsize = min(block_x, w - xoff)

                arr = mask_band.ReadAsArray(xoff, yoff, xsize, ysize)
                if arr is None:
                    continue

                valid = arr > 0
                if not valid.any():
                    continue

                ys, xs = np.where(valid)

                c0 = xoff + int(xs.min())
                c1 = xoff + int(xs.max())
                r0 = yoff + int(ys.min())
                r1 = yoff + int(ys.max())

                min_c = min(min_c, c0)
                max_c = max(max_c, c1)
                min_r = min(min_r, r0)
                max_r = max(max_r, r1)

                n = int(valid.sum())
                valid_count += n
                sum_c += float((xoff + xs).sum())
                sum_r += float((yoff + ys).sum())

        if valid_count == 0 or max_c < 0 or max_r < 0:
            raise RuntimeError("Composite mask indicates no valid data pixels; cannot compute AOI footprint.")

        # Pixel area supports rotation/skew via affine determinant |a*e - b*d|
        pixel_area_units2 = abs(gt[1] * gt[5] - gt[2] * gt[4])
        area_m2 = valid_count * pixel_area_units2 * (meters_per_unit ** 2)
        area_acres = area_m2 / M2_PER_ACRE

        # Pixel sizes (handle rotation)
        px_x_units = math.hypot(gt[1], gt[2])
        px_y_units = math.hypot(gt[4], gt[5])
        px_x_ft = (px_x_units * meters_per_unit) * FT_PER_M
        px_y_ft = (px_y_units * meters_per_unit) * FT_PER_M

        def to_latlon(x: float, y: float) -> Tuple[float, float]:
            lon, lat, *_ = tx.TransformPoint(x, y)
            return (lat, lon)

        # Valid-data bounding box corners in pixel space (outer edge uses max+1)
        ulx, uly = _pixel_corner_to_map(gt, min_c, min_r)
        urx, ury = _pixel_corner_to_map(gt, max_c + 1, min_r)
        lrx, lry = _pixel_corner_to_map(gt, max_c + 1, max_r + 1)
        llx, lly = _pixel_corner_to_map(gt, min_c, max_r + 1)

        corners = {
            "ul": to_latlon(ulx, uly),
            "ur": to_latlon(urx, ury),
            "lr": to_latlon(lrx, lry),
            "ll": to_latlon(llx, lly),
        }

        # Centroid from mean valid pixel indices (mapped using pixel centers)
        mean_c = sum_c / valid_count
        mean_r = sum_r / valid_count
        cx, cy = _pixel_center_to_map(gt, mean_c, mean_r)
        centroid = to_latlon(cx, cy)

        # Compass corners from min/max lat/lon across bounding corners
        lats = [corners[k][0] for k in ("ul", "ur", "lr", "ll")]
        lons = [corners[k][1] for k in ("ul", "ur", "lr", "ll")]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        compass = {
            "nwc": (max_lat, min_lon),
            "nec": (max_lat, max_lon),
            "sec": (min_lat, max_lon),
            "swc": (min_lat, min_lon),
        }

        return {
            "corners": corners,
            "compass": compass,
            "centroid": centroid,
            "area_acres": area_acres,
            "px_x_units": px_x_units,
            "px_y_units": px_y_units,
            "px_x_ft": px_x_ft,
            "px_y_ft": px_y_ft,
        }

    finally:
        ds = None


# =============================================================================
# Main sequencing
# =============================================================================

def iter_source_files(source_dir: Path, recurse: bool) -> list[Path]:
    """
    List candidate source files from the source directory.

    If recurse=False:
      - returns only top-level files in source_dir

    If recurse=True:
      - returns all files under source_dir recursively
    """
    if recurse:
        return [p for p in source_dir.rglob("*") if p.is_file()]
    return [p for p in source_dir.iterdir() if p.is_file()]


def run_sequence(cfg: Config) -> None:
    """
    Run the sequencing operation and write metadata.

    This function:
      - validates directories/paths
      - creates output folder
      - computes AOI once from composite_tif
      - scans source files, filters by extension + naming convention
      - copies to output folder (with overwrite policy)
      - writes ~AerialMetaData.csv
      - prints a summary
    """
    source_dir = cfg.source_dir
    if not source_dir.exists() or not source_dir.is_dir():
        raise FileNotFoundError(f"source_dir is not a valid directory: {source_dir}")

    out_dir = source_dir / cfg.output_subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    # AOI is required for the AOI columns; fail fast if composite is misconfigured
    aoi = compute_aoi_info_from_composite(cfg.composite_tif)

    copied = 0
    existed = 0
    skipped_nonimage = 0
    skipped_nonmatch = 0

    rows: list[list] = []
    candidates = iter_source_files(source_dir, cfg.recurse_source)

    for child in candidates:
        # Never process anything already under the output folder
        try:
            if out_dir.resolve() in child.resolve().parents:
                continue
        except Exception:
            pass

        if child.suffix.lower() not in IMAGE_EXTS:
            skipped_nonimage += 1
            continue

        m = AERIAL_NAME_RE.match(child.stem)
        if not m:
            skipped_nonmatch += 1
            continue

        date_str = m.group(1)
        img_id = int(m.group(2))
        num = f"{img_id:05d}"

        new_name = f"{num}_RockSpringRd_{date_str}_Aerial{cfg.force_output_ext}"
        dest_path = out_dir / new_name

        if dest_path.exists() and not cfg.overwrite_existing:
            existed += 1
        else:
            shutil.copy2(child, dest_path)
            copied += 1


def main() -> None:
    """
    Entry point.

    - Suppresses GDAL console noise
    - Loads SequenceAerialImages.kvp from the script folder
    - Runs sequencing
    """
    gdal.PushErrorHandler("CPLQuietErrorHandler")

    script_dir = Path(__file__).resolve().parent
    cfg = load_config(script_dir)
    run_sequence(cfg)


if __name__ == "__main__":
    main()
