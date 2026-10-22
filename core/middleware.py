import logging
import time

from minty_shared import logctx

api_logger = logging.getLogger("petty-cash-api.http")

#: A path whose rest is a secret (a share link's - ``petty_cash/routes/public_share.py``): logged
#: up to here, never past it (Flask's ``http_hardening.SECRET_PATH_ARGS``).
SECRET_PATH_PREFIXES = ("/api/petty-cash/v1/public/share/",)


def loggable_path(path: str) -> str:
    for prefix in SECRET_PATH_PREFIXES:
        if path.startswith(prefix):
            return prefix + "[redacted]"
    return path


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
    """One trace per request (``minty_shared.logctx``): an incoming ``X-Request-ID`` is kept when
    well formed, echoed on the response, and carried by every line written meanwhile - the auth
    layer adds the user and company, the route name is the fallback step. Then one access line,
    its path through ``loggable_path``."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = logctx.start_trace(request.headers.get(logctx.TRACE_HEADER))
        start = time.monotonic()
        try:
            response = self.get_response(request)
            duration_ms = int((time.monotonic() - start) * 1000)
            response[logctx.TRACE_HEADER] = request.request_id
            path = loggable_path(request.path)
            api_logger.info(
                "%s %s %s %dms",
                request.method,
                path,
                response.status_code,
                duration_ms,
                extra={"endpoint": path, "method": request.method},
            )
            return response
        finally:
            logctx.reset()

    def process_view(self, request, view_func, view_args, view_kwargs):
        logctx.set_default_step(_route_name(request, view_func))
        return None


def _route_name(request, view_func) -> str | None:
    """The step a route gives its request: the ninja operation for THIS method - one URL pattern
    serves every method of a path, under the FIRST operation's URL name, so ``GET /bills/`` would
    read as the create - else the URL name (a plain Django view, or a ninja that changed shape;
    ``test_logging`` pins the ninja case)."""
    for cell in getattr(view_func, "__closure__", None) or ():
        try:
            operations = getattr(cell.cell_contents, "operations", None) or ()
        except ValueError:  # an empty cell
            continue
        for operation in operations:
            if request.method in operation.methods:
                return operation.view_func.__name__
    match = request.resolver_match
    return match.url_name if match else None
