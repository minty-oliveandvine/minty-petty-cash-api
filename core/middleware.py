import logging
import time
import uuid

api_logger = logging.getLogger("petty-cash-api.http")


class NoStoreMiddleware:
    """``Cache-Control: no-store`` on every answer, refusals and failures included - Flask's
    ``petty_cash_api/api.py::_stamp``: a day's figures must never come from a cache."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Cache-Control"] = "no-store"
        return response


class RequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = f"req_{uuid.uuid4().hex[:4]}"
        start = time.time()
        response = self.get_response(request)
        duration_ms = int((time.time() - start) * 1000)
        user_id = getattr(getattr(request, "auth_user", None), "id", "-")
        api_logger.info(
            "%s %dms",
            response.status_code,
            duration_ms,
            extra={
                "request_id": request.request_id,
                "user_id": user_id,
                "endpoint": request.path,
                "method": request.method,
            },
        )
        return response
