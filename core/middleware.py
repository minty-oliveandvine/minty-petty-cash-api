"""This service's own middleware, and the path redaction the shared access line uses
(settings ``MINTY_LOGGABLE_PATH``). The trace and the access line are every API's:
``minty_shared.django.middleware.RequestLoggingMiddleware``."""

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
