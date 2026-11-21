"""FastAPI application: household survey calibration API."""

from __future__ import annotations

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.concurrency import run_in_threadpool

from .errors import ValidationError, WeightingError
from .io_utils import parse_respondent_csv, parse_target_csv
from .models import RespondentBatch, TargetBatch
from .service import WeightingService


_TABLE_ALIASES = {
    "respondent": "respondents",
    "respondents": "respondents",
    "respondent_roster": "respondents",
    "应答名单": "respondents",
    "住户名单": "respondents",
    "target": "targets",
    "targets": "targets",
    "population_targets": "targets",
    "总体目标表": "targets",
    "总体目标": "targets",
}


def _normalize_table(table: str) -> str:
    normalized = _TABLE_ALIASES.get((table or "").strip().lower())
    if normalized is None:
        raise ValidationError(
            "表名必须为 respondents 或 targets", {"table": table}
        )
    return normalized


def _extract_records(payload) -> list:
    """Accept ``{"records": [...]}`` (and close variants) or a bare list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("records", "rows", "data", "entries", "content", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise ValidationError(
        "请求体必须包含记录数组（records）",
        {"payload_type": type(payload).__name__},
    )


def _coerce_version(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError("版号必须为整数", {"version": value})


def _version_from_payload(payload) -> int | None:
    if isinstance(payload, dict):
        for key in ("version", "table_version", "tableVersion", "版号"):
            if payload.get(key) is not None:
                return _coerce_version(payload[key])
    return None


def _table_from_payload(payload) -> str | None:
    if isinstance(payload, dict):
        for key in ("table", "table_name", "tableName", "表名"):
            if payload.get(key):
                return _normalize_table(str(payload[key]))
    return None


def _parse_verification_records(table: str, payload) -> list[dict]:
    model = RespondentBatch if table == "respondents" else TargetBatch
    try:
        batch = model.model_validate({"records": _extract_records(payload)})
    except PydanticValidationError as exc:
        raise RequestValidationError(exc.errors())
    return [r.model_dump() for r in batch.records]


async def _uploaded_content(form) -> bytes:
    for key in ("file", "upload", "csv", "data"):
        value = form.get(key)
        if value is not None and not isinstance(value, str):
            return await value.read()
        if isinstance(value, str) and value:
            return value.encode("utf-8")
    raise ValidationError("缺少待核对的 CSV 文件", {"field": "file"})


async def _parse_csv_form(table: str, form) -> list[dict]:
    content = await _uploaded_content(form)
    return (
        parse_respondent_csv(content)
        if table == "respondents"
        else parse_target_csv(content)
    )


def _is_multipart(request: Request) -> bool:
    content_type = request.headers.get("content-type", "").lower()
    return (
        "multipart/form-data" in content_type
        or "application/x-www-form-urlencoded" in content_type
    )


async def _run_verification(
    request: Request,
    table: str | None,
    version: int | None,
) -> dict:
    """Shared handler for the verification endpoints.

    Accepts JSON (``{"records": [...]}``, optionally with ``table`` and
    ``version``) or a CSV upload (multipart with ``file``).  Table and
    version may come from the path, the query string, the form or the JSON
    body, in that order of precedence.
    """
    if _is_multipart(request):
        form = await request.form()
        if table is None:
            table = str(form.get("table") or form.get("table_name") or "")
        normalized = _normalize_table(table)
        if version is None:
            version = _coerce_version(
                form.get("version") or form.get("table_version")
            )
        if version is None:
            raise ValidationError(
                "必须指明待核对的版号（version）", {"table": normalized}
            )
        records = await _parse_csv_form(normalized, form)
        return await run_in_threadpool(
            request.app.state.service.verify_table,
            normalized,
            version,
            records,
        )

    try:
        payload = await request.json()
    except Exception:
        raise ValidationError("请求体必须为 JSON 或 CSV 上传")
    if table is None:
        table = _table_from_payload(payload)
        if table is None:
            raise ValidationError(
                "请求体必须指明表名（table）", {"table": None}
            )
    normalized = _normalize_table(table)
    if version is None:
        version = _version_from_payload(payload)
    if version is None:
        raise ValidationError(
            "必须指明待核对的版号（version）", {"table": normalized}
        )
    records = _parse_verification_records(normalized, payload)
    return await run_in_threadpool(
        request.app.state.service.verify_table,
        normalized,
        version,
        records,
    )


def create_app(service: WeightingService | None = None) -> FastAPI:
    app = FastAPI(
        title="住户出行调查配权 API",
        description=(
            "应答户权重校准服务。两份源表（应答名单、总体目标表）支持整份换交，"
            "配权采用两阶段线性规划：先最小化各组加权户数与目标的绝对差之和，"
            "再最小化各户权重相对初始权重的绝对改动之和，"
            "每户权重限制在初始权重的 0.5~2 倍。"
        ),
        version="1.0.0",
    )
    # Lazily build the default service on first use so importing the module
    # never touches the filesystem (keeps tests and tooling side-effect free).
    app.state._injected_service = service

    class _ServiceHolder:
        def __getattr__(self, name):
            if app.state._injected_service is None:
                app.state._injected_service = WeightingService()
            return getattr(app.state._injected_service, name)

    app.state.service = _ServiceHolder()

    @app.exception_handler(WeightingError)
    async def _domain_error_handler(request: Request, exc: WeightingError):
        # Plain Response so TestClient does not re-raise the exception.
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ):
        return JSONResponse(
            status_code=400,
            content=jsonable_encoder({
                "error": "invalid_payload",
                "message": "请求数据校验失败",
                "details": {"validation": exc.errors()},
            }),
        )

    @app.get("/health", tags=["系统"])
    async def health():
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Table replacement (whole snapshot swap)
    # ------------------------------------------------------------------
    @app.post("/tables/respondents", tags=["源表换交"])
    async def replace_respondents_json(batch: RespondentBatch):
        records = [r.model_dump() for r in batch.records]
        return app.state.service.replace_respondents(records)

    @app.post("/tables/respondents/csv", tags=["源表换交"])
    async def replace_respondents_csv(file: UploadFile = File(...)):
        content = await file.read()
        records = parse_respondent_csv(content)
        return app.state.service.replace_respondents(records)

    @app.post("/tables/targets", tags=["源表换交"])
    async def replace_targets_json(batch: TargetBatch):
        records = [r.model_dump() for r in batch.records]
        return app.state.service.replace_targets(records)

    @app.post("/tables/targets/csv", tags=["源表换交"])
    async def replace_targets_csv(file: UploadFile = File(...)):
        content = await file.read()
        records = parse_target_csv(content)
        return app.state.service.replace_targets(records)

    @app.get("/tables/versions", tags=["源表换交"])
    async def table_versions():
        return app.state.service.get_versions()

    # ------------------------------------------------------------------
    # Verification of current snapshots (legacy tables)
    # ------------------------------------------------------------------
    @app.get("/tables/verifications", tags=["源表核对"])
    async def table_verifications():
        return app.state.service.get_verifications()

    @app.post("/tables/verify", tags=["源表核对"])
    async def verify_generic(
        request: Request,
        version: int | None = Query(default=None, description="待核对的入库版号"),
    ):
        """Verify a table named in the body/form (JSON or CSV)."""
        return await _run_verification(request, None, version)

    @app.post("/tables/{table}/verify", tags=["源表核对"])
    async def verify_table(
        request: Request,
        table: str,
        version: int | None = Query(default=None, description="待核对的入库版号"),
    ):
        """Verify one table; accepts JSON records or a CSV upload."""
        return await _run_verification(request, table, version)

    @app.post("/tables/{table}/verify/csv", tags=["源表核对"])
    async def verify_table_csv(
        request: Request,
        table: str,
        version: int | None = Query(default=None, description="待核对的入库版号"),
    ):
        """CSV-only alias of the verification endpoint."""
        return await _run_verification(request, table, version)

    # ------------------------------------------------------------------
    # Weighting jobs
    # ------------------------------------------------------------------
    @app.post("/weighting/jobs", tags=["配权作业"])
    async def start_weighting():
        return app.state.service.start_weighting()

    @app.get("/weighting/status", tags=["配权作业"])
    async def weighting_status():
        return app.state.service.get_status()

    @app.get("/weighting/weights", tags=["结果领取"])
    async def household_weights(
        limit: int | None = Query(default=None, ge=1),
        offset: int = Query(default=0, ge=0),
    ):
        return app.state.service.get_weights(limit=limit, offset=offset)

    @app.get("/weighting/differences", tags=["结果领取"])
    async def group_differences():
        return app.state.service.get_differences()

    return app


app = create_app()
