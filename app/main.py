from contextlib import asynccontextmanager
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import paddle, web

from config import settings

from app.paddle import Paddle
from app.website import ClaimLimits, ClaimWaiters

from events import EventPublisher, close_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup logic ---
    app.state.publisher = EventPublisher()
    app.state.paddle = Paddle()
    # One shared reader of licenses:issued for every waiting claim (app/website/claims.py)
    app.state.claims = ClaimWaiters()
    await app.state.claims.start()
    app.state.claim_limits = ClaimLimits(
        max_waits_per_ip=settings.CLAIM_MAX_WAITS_PER_IP,
        max_requests_per_minute=settings.CLAIM_MAX_REQUESTS_PER_MINUTE,
        max_waits=settings.CLAIM_MAX_WAITS,
        wait_seconds=settings.CLAIM_WAIT_SECONDS,
    )

    yield

    # --- shutdown logic ---
    await app.state.claims.stop()
    del app.state.claims
    del app.state.claim_limits
    del app.state.publisher
    del app.state.paddle
    await close_redis()


api_prefix = f"/{settings.API_NAME}/{settings.API_VERSION}"

app = FastAPI(
    title=settings.APP_NAME,
    description=settings.APP_DESCRIPTION,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url=f"{api_prefix}/docs",
    redoc_url=f"{api_prefix}/redoc",
    swagger_ui_oauth2_redirect_url=f"{api_prefix}/docs/oauth2-redirect",
    openapi_url=f"{api_prefix}/openapi.json",
)


# The website calls the web routes from the buyer's browser. CORS only stops other sites' pages: scripts
# ignore it, the claim limits (app/website/limits.py) are what protects the route
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.WEBSITE_ORIGIN],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)


# Include routers
api = APIRouter(prefix=api_prefix)
api.include_router(paddle.router)
api.include_router(web.router)
app.include_router(api)