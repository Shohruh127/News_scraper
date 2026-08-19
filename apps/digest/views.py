"""HTTP views for health and readiness endpoints."""

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from . import health


@require_GET
def healthz(request) -> JsonResponse:
    """Liveness probe: returns 200 OK immediately if process is running."""
    data = health.check_liveness()
    return JsonResponse(data, status=200)


@require_GET
def readyz(request) -> JsonResponse:
    """Readiness probe: returns 200 OK if DB, Redis, and schema are ready, 503 otherwise."""
    is_ready, data = health.check_readiness()
    status_code = 200 if is_ready else 503
    return JsonResponse(data, status=status_code)


@require_GET
def runtime_health_view(request) -> JsonResponse:
    """Detailed runtime health view."""
    strict = request.GET.get("strict", "").lower() in ("true", "1", "yes")
    is_healthy, data = health.check_runtime_health(strict=strict)
    status_code = 200 if is_healthy else 503
    return JsonResponse(data, status=status_code)
