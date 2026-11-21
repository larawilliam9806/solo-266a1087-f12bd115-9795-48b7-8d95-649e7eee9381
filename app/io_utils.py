"""CSV parsing for whole-table replacement uploads.

CSV files are untrusted input; every cell is validated here and turned into
the same plain-dict shape the JSON path produces.
"""

from __future__ import annotations

import csv
import io

from .errors import ValidationError

RESPONDENT_COLUMNS = ("household_id", "district", "size_band", "init_weight")
TARGET_COLUMNS = ("dimension", "group_name", "target_count")


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("CSV 文件必须为 UTF-8 编码")


def parse_respondent_csv(content: bytes) -> list[dict]:
    text = _decode(content)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValidationError("CSV 内容为空或缺少表头")
    header = [h.strip() for h in reader.fieldnames]
    missing = [c for c in RESPONDENT_COLUMNS if c not in header]
    if missing:
        raise ValidationError(
            "应答名单 CSV 缺少必需列", {"missing_columns": missing}
        )

    rows: list[dict] = []
    seen: set[str] = set()
    for line, raw in enumerate(reader, start=2):
        try:
            hid = (raw.get("household_id") or "").strip()
            district = (raw.get("district") or "").strip()
            band = (raw.get("size_band") or "").strip()
            w_raw = (raw.get("init_weight") or "").strip()
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
    text = _decode(content)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValidationError("CSV 内容为空或缺少表头")
    header = [h.strip() for h in reader.fieldnames]
    missing = [c for c in TARGET_COLUMNS if c not in header]
    if missing:
        raise ValidationError(
            "总体目标表 CSV 缺少必需列", {"missing_columns": missing}
        )

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line, raw in enumerate(reader, start=2):
        try:
            dimension = (raw.get("dimension") or "").strip()
            group = (raw.get("group_name") or "").strip()
            t_raw = (raw.get("target_count") or "").strip()
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
