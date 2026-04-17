from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.middleware.body_limit import BodySizeLimitMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.redis import close_redis
from app.routers import alerts, auth_routes, cues, device_code, echo, executions, health, usage, webhook_secret, workers
from app.utils.logging import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    # Run alembic migrations on startup
    import subprocess, sys, logging
    logger = logging.getLogger(__name__)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            capture_output=True, text=True, timeout=60
        )
        logger.info("Alembic migration stdout: %s", result.stdout)
        if result.returncode != 0:
            logger.error("Alembic migration failed (rc=%d): %s", result.returncode, result.stderr)
        else:
            logger.info("Alembic migrations applied successfully")
    except Exception as e:
        logger.error("Alembic migration exception: %s", e)
    yield
    await close_redis()


openapi_tags = [
    {"name": "auth", "description": "Authentication: register, login (device code flow), API key management"},
    {"name": "cues", "description": "CRUD operations for scheduled cues (tasks)"},
    {"name": "executions", "description": "Execution management: outcomes, claimable list, claim for worker transport"},
    {"name": "worker", "description": "Worker transport: heartbeat registration for pull-based execution delivery"},
    {"name": "usage", "description": "Usage stats and plan information"},
    {"name": "echo", "description": "Echo endpoint for testing webhook delivery"},
    {"name": "alerts", "description": "Persisted alerts fired by outcome service; optional signed webhook delivery via alert_webhook_url"},
    {"name": "auth-pages", "description": "HTML pages for device code verification flow"},
]

import os
_ENV = os.getenv("ENV", "development")

app = FastAPI(
    title="CueAPI",
    version="1.0.0",
    description="Scheduling infrastructure for AI agents. Register cues (scheduled tasks), CueAPI fires webhooks or delivers via worker pull at the right time.",
    lifespan=lifespan,
    openapi_tags=openapi_tags,
    docs_url="/docs" if _ENV != "production" else None,
    redoc_url="/redoc" if _ENV != "production" else None,
    openapi_url="/openapi.json" if _ENV != "production" else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After", "X-CueAPI-Usage-Warning"],
)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(RequestIdMiddleware)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Unwrap CueAPI error format from HTTPException detail."""
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        return JSONResponse(status_code=exc.status_code, content=detail)
    if isinstance(detail, str):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "http_error",
                    "message": detail,
                    "status": exc.status_code,
                }
            },
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Return CueAPI-format error for Pydantic validation failures."""
    errors = exc.errors()

    if errors and errors[0].get("type") == "json_invalid":
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_json",
                    "message": "Request body is not valid JSON",
                    "status": 400,
                }
            },
        )

    details = []
    for err in errors:
        loc = ".".join(str(x) for x in err.get("loc", []))
        details.append({"field": loc, "message": err.get("msg", "")})
    first = errors[0] if errors else {}
    field = ".".join(str(x) for x in first.get("loc", []))
    msg = first.get("msg", "Validation error")
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": f"{field}: {msg}" if field else msg,
                "status": 422,
                "details": details,
            }
        },
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    """Catch unhandled exceptions and return CueAPI-format 500."""
    import logging
    logging.getLogger(__name__).exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "An internal error occurred",
                "status": 500,
            }
        },
    )


app.include_router(health.router)
app.include_router(auth_routes.router)
app.include_router(device_code.router)
app.include_router(device_code.page_router)
app.include_router(cues.router)
app.include_router(executions.router)
app.include_router(usage.router)
app.include_router(echo.router)
app.include_router(workers.router)
app.include_router(workers.workers_list_router)
app.include_router(webhook_secret.router)
app.include_router(alerts.router)
