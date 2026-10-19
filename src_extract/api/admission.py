"""Bound public generation requests before uploads, authentication or filters.

The execution pool separately retains capacity for abandoned running workers.
These are per-process limits; no accounting/credential state is changed.
"""
from starlette.responses import JSONResponse
from services.runtime_configuration import env_int


class GenerationAdmissionMiddleware:
    PATHS = {"/v1/images/generations", "/v1/images/edits",
             "/v1/chat/completions", "/v1/responses"}

    def __init__(self, app, capacity=None):
        self.app = app
        self.capacity = capacity or (
            env_int("CHATGPT2API_IMAGE_THREAD_TOKENS", 100, 1, 2000)
            + env_int("CHATGPT2API_IMAGE_QUEUE_LIMIT", 40, 0, 10000))
        self.active = 0

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or scope.get("path") not in self.PATHS):
            return await self.app(scope, receive, send)
        if self.active >= self.capacity:
            return await JSONResponse({"error": {"type": "server_error",
                "code": "image_execution_busy", "message": "Generation queue is full; retry shortly."}},
                status_code=503, headers={"Retry-After": "2"})(scope, receive, send)
        # No await between checking/incrementing: atomic on the serving loop.
        self.active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.active -= 1
