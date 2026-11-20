import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import router
from .config import settings

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Document Processing Service",
    version="1.0.0",
    description="Upload PDFs/images; text is extracted asynchronously by background workers.",
)
app.include_router(router)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal server error."}, status_code=500)
