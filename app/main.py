"""FastAPI application: household survey calibration API."""

from __future__ import annotations

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .errors import WeightingError
from .io_utils import parse_respondent_csv, parse_target_csv
from .models import RespondentBatch, TargetBatch
from .service import WeightingService


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
