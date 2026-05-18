"""
bm-analysis-service — FastAPI application entry point.
"""
import logging
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.routers import (
    analyses,
    audio_analysis,
    criteria,
    drafts,
    health,
    prompt_builder,
    prompts,
    transcription_analysis,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="BM Analysis Service",
    description="Backend for Boston Medical call analysis — manages prompts, criteria, and analysis results.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
origins = settings.allowed_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global error handler ──────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url)
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "status": "error",
            "error_message": "Internal server error. Please check service logs.",
        },
    )

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(prompts.router)
app.include_router(criteria.router)
app.include_router(drafts.router)
app.include_router(analyses.router)
app.include_router(audio_analysis.router)
app.include_router(transcription_analysis.router)
app.include_router(prompt_builder.router)

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    logger.info("bm-analysis-service starting up")
    logger.info("AI provider: azure_openai")
    logger.info("Azure text configured: %s", "yes" if settings.azure_openai_text_endpoint and settings.azure_openai_text_deployment else "no")
    logger.info("Azure audio configured: %s", "yes" if settings.azure_openai_audio_endpoint and settings.azure_openai_audio_deployment else "no")
    logger.info("Azure transcription configured: %s", "yes" if settings.azure_openai_transcription_endpoint and settings.azure_openai_transcription_deployment else "no")
    logger.info("CORS origins: %s", settings.allowed_origins)
    if not settings.database_url:
        logger.warning("DATABASE_URL is not set!")
