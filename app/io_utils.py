"""CSV parsing for whole-table replacement uploads.

CSV files are untrusted input; every cell is validated here and turned into
the same plain-dict shape the JSON path produces.

Headers are compared after trimming surrounding whitespace.  A required
column that appears more than once (for example ``district, district``) is
rejected outright: ``csv.DictReader`` would silently keep the last column
and let a mislabelled upload switch the live version.  Naming the offending
column in the error makes the upload fixable.
"""

from __future__ import annotations

import csv
import io
from collections import Counter

from .errors import ValidationError

RESPONDENT_COLUMNS = ("household_id", "district", "size_band", "init_weight")
TARGET_COLUMNS = ("dimension", "group_name", "target_count")


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("CSV 文件必须为 UTF-8 编码")


def _open_csv(
    content: bytes, required: tuple[str, ...], label: str
) -> tuple[csv.reader, dict[str, int]]:
    """Validate the header and return a row reader plus column positions.

    Duplicate required columns are rejected after stripping whitespace, and
    the error names every duplicated column.
    """
    text = _decode(content)
    reader = csv.reader(io.StringIO(text))
    try:
        raw_header = next(reader)
    except StopIteration:
        raise ValidationError(f"{label} CSV 内容为空或缺少表头")

    header = [h.strip() for h in raw_header]
    if not header:
        raise ValidationError(f"{label} CSV 内容为空或缺少表头")

    counts = Counter(h for h in header if h in required)
    duplicate_columns = [c for c in required if counts[c] > 1]
    if duplicate_columns:
        names = ", ".join(duplicate_columns)
        raise ValidationError(
            f"{label} CSV 必需列重复: {names}",
            {"duplicate_columns": duplicate_columns},
        )

    missing = [c for c in required if c not in header]
    if missing:
        raise ValidationError(
            f"{label} CSV 缺少必需列", {"missing_columns": missing}
        )

    positions = {c: header.index(c) for c in required}
    return reader, positions


def _row(raw: list[str], positions: dict[str, int]) -> dict[str, str]:
    """Map a raw CSV row to trimmed values, tolerating short/extra cells."""
    return {
        name: (raw[idx].strip() if idx < len(raw) else "")
        for name, idx in positions.items()
    }


def parse_respondent_csv(content: bytes) -> list[dict]:
    reader, positions = _open_csv(
        content, RESPONDENT_COLUMNS, "应答名单"
    )

    rows: list[dict] = []
    seen: set[str] = set()
    for line, raw_row in enumerate(reader, start=2):
        raw = _row(raw_row, positions)
        try:
            hid = raw["household_id"]
            district = raw["district"]
            band = raw["size_band"]
            w_raw = raw["init_weight"]
            if not hid or not district or not band or not w_raw:
                raise ValueError("存在空字段")
            try:
                weight = float(w_raw)
            except ValueError:
                raise ValueError(f"init_weight 不是数字: {w_raw!r}")
            if weight != weight or weight in (float("inf"), float("-inf")):
                raise ValueError("init_weight 必须为有限数")
            if weight <= 0:
                raise ValueError("init_weight 必须为正数")
            if hid in seen:
                raise ValueError(f"户号重复: {hid}")
            seen.add(hid)
            rows.append(
                {
                    "household_id": hid,
                    "district": district,
                    "size_band": band,
                    "init_weight": weight,
                }
            )
        except ValueError as exc:
            raise ValidationError(
                f"应答名单 CSV 第 {line} 行有误: {exc}",
                {"line": line, "reason": str(exc)},
            )
    if not rows:
        raise ValidationError("应答名单 CSV 没有任何数据行")
    return rows


def parse_target_csv(content: bytes) -> list[dict]:
    reader, positions = _open_csv(content, TARGET_COLUMNS, "总体目标表")

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line, raw_row in enumerate(reader, start=2):
        raw = _row(raw_row, positions)
        try:
            dimension = raw["dimension"]
            group = raw["group_name"]
            t_raw = raw["target_count"]
            if dimension not in ("district", "size_band"):
                raise ValueError(
                    f"dimension 必须是 district 或 size_band, 实际为 {dimension!r}"
                )
            if not group or not t_raw:
                raise ValueError("存在空字段")
            try:
                target = float(t_raw)
            except ValueError:
                raise ValueError(f"target_count 不是数字: {t_raw!r}")
            if target != target or target in (float("inf"), float("-inf")):
                raise ValueError("target_count 必须为有限数")
            if target < 0:
                raise ValueError("target_count 必须非负")
            key = (dimension, group)
            if key in seen:
                raise ValueError(f"分组目标重复: {dimension}/{group}")
            seen.add(key)
            rows.append(
                {
                    "dimension": dimension,
                    "group_name": group,
                    "target_count": target,
                }
            )
        except ValueError as exc:
            raise ValidationError(
                f"总体目标表 CSV 第 {line} 行有误: {exc}",
                {"line": line, "reason": str(exc)},
            )
    if not rows:
        raise ValidationError("总体目标表 CSV 没有任何数据行")
    return rows
