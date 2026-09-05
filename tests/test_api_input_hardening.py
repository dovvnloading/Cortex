"""Untrusted input must fail with a real status code, not a 500 or an OOM.

Each of these was reachable with a single ordinary request, and each failed in
a way that told the caller nothing useful -- or, in the resize case, spent two
gigabytes before failing.
"""

from __future__ import annotations

from pathlib import Path
import tempfile

from fastapi.testclient import TestClient
import pytest

from cortex_backend.execution.recipes import (
    MAX_PIXELS,
    RecipeValidationError,
    parse_image_transform,
)


def _plan(steps: list[dict]) -> dict:
    return {
        "schema_version": "artifact.transform.v1",
        "input_artifact_id": "artifact-1",
        "steps": steps,
        "output_format": "png",
    }


def test_a_resize_bounded_per_side_is_still_bounded_by_area() -> None:
    """16384 x 16384 passed both per-side limits and asked for ~2.2 GB.

    A few hundred bytes of JSON made a worker allocate until it died, and the
    request is trivially repeatable.
    """
    with pytest.raises(RecipeValidationError):
        parse_image_transform(_plan([{"op": "resize", "width": 16384, "height": 16384}]))

    # A plan inside the budget still parses.
    ok = parse_image_transform(_plan([{"op": "resize", "width": 1024, "height": 1024}]))
    assert ok.steps[0].width == 1024


def test_a_crop_region_is_bounded_by_area_too() -> None:
    with pytest.raises(RecipeValidationError):
        parse_image_transform(
            _plan([{"op": "crop", "x": 0, "y": 0, "width": 16384, "height": 16384}])
        )


def test_the_pixel_budget_matches_the_provider_that_enforces_it() -> None:
    """Parse-time and run-time ceilings must not drift apart."""
    from cortex_backend.execution.recipe_provider import MAX_PIXELS as PROVIDER_MAX

    assert MAX_PIXELS == PROVIDER_MAX


@pytest.fixture
def client():
    import app_factory

    app = app_factory.build_app(
        data_dir=Path(tempfile.mkdtemp()), serve_frontend=False, handoff_secret="probe"
    )
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as c:
        token = app.state.session_manager.bootstrap_token
        exchanged = c.post("/api/v1/session/exchange", json={"bootstrap_token": token})
        c.headers.update(
            {"Authorization": f"Bearer {exchanged.json()['session_token']}"}
        )
        yield c


@pytest.mark.parametrize(
    ("raw_host", "expected"),
    [
        ("[::1", ""),
        ("[", ""),
        ("[:evil.com", ""),
        # urlsplit raises on this from 3.12, but returns "::1" on 3.10 and
        # 3.11 -- including the 3.11 this ships on -- so trailing junk after a
        # bracketed literal used to pass the host allowlist. CI caught the
        # difference; the parser no longer depends on the interpreter version.
        ("[::1]evil.com", ""),
        ("[::1]", "::1"),
        ("[::1]:8080", "::1"),
        ("127.0.0.1", "127.0.0.1"),
        ("localhost:5173", "localhost"),
    ],
)
def test_the_host_parser_agrees_on_every_python_version(raw_host: str, expected: str) -> None:
    from cortex_backend.api.security import _parse_host_header

    assert _parse_host_header(raw_host) == expected


@pytest.mark.parametrize("raw_host", ["[::1", "[", "[:evil.com", "[::1]evil.com"])
def test_a_malformed_host_header_is_a_400_not_a_500(client, raw_host: str) -> None:
    """urlsplit raises on an unbalanced bracket.

    This check runs before any credential is examined, on every route, so an
    uncaught error here was an unauthenticated 500 with a traceback in the log.
    """
    response = client.get("/api/v1/health/live", headers={"Host": raw_host})

    assert response.status_code == 400, (
        f"Host: {raw_host!r} produced {response.status_code}"
    )


def test_an_out_of_range_last_event_id_is_refused_before_the_stream_opens(client) -> None:
    """SQLite cannot bind above 2**63-1.

    The OverflowError landed inside the streaming generator, after the 200 and
    its headers were already sent, so the client saw a successful response
    with a truncated body that never terminated.
    """
    accepted = client.post(
        "/api/v1/execution/scratch",
        json={"request_id": "overflow-1", "expression": "1 + 1"},
    )
    job_id = accepted.json()["job_id"]

    response = client.get(
        f"/api/v1/execution/{job_id}/events",
        headers={"Last-Event-ID": str(2**63)},
    )

    assert response.status_code == 400


def test_an_oversized_memory_is_refused_the_same_way_by_put_and_post(client) -> None:
    """POST bounded each item; its PUT sibling did not, so PUT answered 500."""
    assert client.post("/api/v1/memories", json={"memo": "x" * 501}).status_code == 422
    assert client.put("/api/v1/memories", json={"memos": ["x" * 501]}).status_code == 422


def test_a_full_memory_store_is_a_conflict_not_a_server_fault(client) -> None:
    """Reaching the limit is an expected outcome the user can act on."""
    response = None
    for index in range(101):
        response = client.post("/api/v1/memories", json={"memo": f"memory {index}"})

    assert response is not None
    assert response.status_code == 409
    assert "100" in response.json()["detail"]
