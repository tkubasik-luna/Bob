"""CORS preflight for the cross-origin REST API (PRD 0012).

The Sphere HUD webview calls `/api/llm/*` from a different origin (Vite :1420 in
dev, the Tauri webview origin packaged), so the browser preflights the blocking
`PUT /api/llm/selection` with `OPTIONS`. Without CORS the preflight 405s and the
picker can never reach `PUT` — this guards that regression.

The preflight (`OPTIONS`) is handled by the CORS middleware before any route logic,
so it needs no lifespan/DI; the route bodies are covered by ``test_llm_router_put``.
"""

from fastapi.testclient import TestClient

from bob.main import app

_DEV_ORIGIN = "http://127.0.0.1:1420"


def test_put_selection_preflight_allowed() -> None:
    client = TestClient(app)
    response = client.options(
        "/api/llm/selection",
        headers={
            "Origin": _DEV_ORIGIN,
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == _DEV_ORIGIN


def test_unknown_origin_not_allowed() -> None:
    client = TestClient(app)
    response = client.options(
        "/api/llm/selection",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert "access-control-allow-origin" not in response.headers
