"""URL mounting.

THE PATHS ARE FLASK'S: every route is under ``/api/petty-cash/v1/companies/{entity_id}``,
exactly where Flask's ``blueprints/petty_cash_api`` serves it, so minty-petty-cash-web moves
here by changing ``PETTY_CASH_API_URL`` alone.

AUTH DEFAULTS TO ON: ``NinjaAPI(auth=PettyCashBearerAuth())`` gates every endpoint, so a new
route that forgets nothing still fails closed. Public: ``/healthz`` and the OpenAPI document.
"""

from django.http import JsonResponse
from django.urls import path
from ninja import NinjaAPI

from core.auth import PettyCashBearerAuth
from core.exceptions import FAILED, register_exception_handlers

api = NinjaAPI(
    title="Minty Petty Cash API",
    version="0.1.0",
    description="The day's petty cash report for minty-petty-cash-web (the Flask blueprint's paths).",
    auth=PettyCashBearerAuth(),
    urls_namespace="petty_cash",
    openapi_url="/openapi.json",
    docs_url=None,
)

register_exception_handlers(api)

from petty_cash.api import routers  # noqa: E402

for prefix, router, tag in routers:
    api.add_router(prefix, router, tags=[tag])


def healthz(request):
    """Liveness only: no database, no token (the entrypoint already waited for the schema)."""
    return JsonResponse({"status": "ok", "service": "minty-petty-cash-api"})


urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("api/petty-cash/v1/", api.urls),
]


def _not_found(request, exception=None):
    """An unknown path is a JSON refusal, as Flask's blueprint answers it - never an HTML page."""
    return JsonResponse({"error": "Not Found"}, status=404)


def _server_error(request):
    return JsonResponse({"error": FAILED}, status=500)


handler404 = _not_found
handler500 = _server_error
