"""Rate limiting and error handling."""

import logging
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("agos")

# Basic in-memory rate limiting per client IP.
_hits: dict = defaultdict(deque)
LIMITS = {"/api/waitlist": (5, 60), "default": (240, 60)}


async def rate_limit(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") and not path.startswith("/api/__clerk"):
        ip = (
            (request.headers.get("x-forwarded-for") or (request.client.host if request.client else ""))
            .split(",")[0]
            .strip()
        )
        key = path if path in LIMITS else "default"
        limit, window = LIMITS[key]
        if key != "default" and request.method == "GET":
            key, (limit, window) = "default", LIMITS["default"]
        q = _hits[(ip, key)]
        now = time.monotonic()
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return JSONResponse({"detail": "Too many requests; slow down"}, status_code=429)
        q.append(now)
    return await call_next(request)


async def safe_errors(request: Request, exc: Exception):
    logger.error("Unhandled error on %s: %s", request.url.path, type(exc).__name__)
    return JSONResponse({"detail": "Something went wrong. Please try again."}, status_code=500)
