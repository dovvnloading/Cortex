"""Tag-only admission checks must not fetch rich Ollama metadata N times."""

from __future__ import annotations

from cortex_backend.services.model_catalog import CombinedModelCatalog
from cortex_backend.services.models import InstalledModel, ModelService


class _CountingOllama:
    def __init__(self, count: int = 80) -> None:
        self.names = [f"local-tag-{index}:latest" for index in range(count)]
        self.list_calls = 0
        self.show_calls: list[str] = []

    def list(self):
        self.list_calls += 1
        return {"models": [{"name": name, "size": 128} for name in reversed(self.names)]}

    def show(self, model: str):
        self.show_calls.append(model)
        return {
            "capabilities": ["completion", "vision"],
            "details": {"family": "llama"},
            "model_info": {"llama.context_length": 8192},
        }

    def pull(self, model: str, *, stream: bool = False):
        raise AssertionError(f"Availability listing must never pull {model}")


class _FakeGGUF:
    def __init__(self) -> None:
        self.calls = 0

    def list_installed_details(self) -> tuple[InstalledModel, ...]:
        self.calls += 1
        return (InstalledModel(name="gguf:local.Q4_K.gguf", source="gguf"),)


def test_model_service_list_installed_is_one_list_without_any_show_calls() -> None:
    gateway = _CountingOllama()
    models = ModelService(gateway)

    tags = models.list_installed()

    assert len(tags) == 80
    assert tags == tuple(sorted(gateway.names))
    assert gateway.list_calls == 1
    assert gateway.show_calls == []


def test_explicit_rich_inventory_still_fetches_show_metadata() -> None:
    gateway = _CountingOllama(count=3)
    models = ModelService(gateway)

    rich, connection = models.inventory()

    assert connection.success is True
    assert {item.name for item in rich} == set(gateway.names)
    assert all(item.supports_vision and item.context_length == 8192 for item in rich)
    assert gateway.list_calls == 1
    assert sorted(gateway.show_calls) == sorted(gateway.names)


def test_combined_catalog_tag_only_union_never_expands_ollama_metadata() -> None:
    gateway = _CountingOllama()
    gguf = _FakeGGUF()
    catalog = CombinedModelCatalog(ModelService(gateway), gguf)

    names = catalog.list_installed()

    assert names == (*sorted(gateway.names), "gguf:local.Q4_K.gguf")
    assert gateway.list_calls == 1
    assert gateway.show_calls == []
    assert gguf.calls == 1

    rich, connection = catalog.inventory()
    assert connection.success is True
    assert len(rich) == 81
    assert len(gateway.show_calls) == 80
    assert gguf.calls == 2


def test_combined_catalog_keeps_ollama_tags_when_gguf_scan_fails(caplog) -> None:
    class _BrokenGGUF:
        def list_installed_details(self):
            raise OSError("secret-local-path")

    gateway = _CountingOllama(count=2)
    catalog = CombinedModelCatalog(ModelService(gateway), _BrokenGGUF())
    names = catalog.list_installed()

    assert names == tuple(sorted(gateway.names))
    assert gateway.list_calls == 1
    assert gateway.show_calls == []
    assert "secret-local-path" not in caplog.text


def test_unreachable_ollama_keeps_tag_only_fallback_empty_and_safe(caplog) -> None:
    class _Unavailable:
        def list(self):
            raise ConnectionError("sensitive-host-or-user-data")

        def show(self, model):
            raise AssertionError("show should never be called")

    assert ModelService(_Unavailable()).list_installed() == ()
    assert "sensitive-host-or-user-data" not in caplog.text
