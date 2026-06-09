"""Smoke test for /health endpoint."""

import pytest
from fastapi.testclient import TestClient

from bob.connectors.mcp import MCPRuntime
from bob.main import app
from bob.sub_agent.tool_registry import SubAgentToolRegistry


def test_health_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_reports_mcp_startup_failure(
    clear_jarvis_history: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An MCP runtime startup failure is visible on /health (issue 0124).

    The boot survives (the catch-all in the lifespan), but the degradation is
    recorded in app state and exposed instead of living only in the boot log.
    """

    async def _boom(self: MCPRuntime, registry: SubAgentToolRegistry) -> dict[str, list[str]]:
        raise RuntimeError("mcp runtime exploded")

    monkeypatch.setattr(MCPRuntime, "startup", _boom)
    try:
        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json() == {
                "status": "degraded",
                "mcp_startup_error": "RuntimeError: mcp runtime exploded",
            }
    finally:
        # The flag lives on the FastAPI app object (process-wide); clear it so
        # an unrelated later test never observes this test's injected failure.
        app.state.mcp_startup_error = None


def test_health_recovers_on_clean_boot(clear_jarvis_history: None) -> None:
    """A clean lifespan boot resets a previously-recorded MCP startup error."""

    app.state.mcp_startup_error = "RuntimeError: stale from a previous boot"
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.json() == {"status": "ok"}
