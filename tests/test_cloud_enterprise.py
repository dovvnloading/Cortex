import httpx
import pytest

from cortex_backend.services.cloud_catalog import get_provider, list_cloud_models
from cortex_backend.services.cloud_enterprise import enterprise_headers
from cortex_backend.services.cloud_types import CloudProviderError


def test_cloudflare_pages_through_the_accounts_text_generation_catalog(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test")
    monkeypatch.setenv("CORTEX_CLOUDFLARE_BASE_URL", "https://example.test/accounts/a/ai/v1")
    requests = []

    def handler(request):
        requests.append(request)
        page = request.url.params["page"]
        assert request.url.path == "/accounts/a/ai/models/search"
        assert request.url.params["task"] == "Text Generation"
        return httpx.Response(200, json={"result": [{"name": "@cf/test/model-" + page}], "result_info": {"total_pages": 2}})

    assert list_cloud_models("cloudflare", transport=httpx.MockTransport(handler)) == ["@cf/test/model-1", "@cf/test/model-2"]
    assert len(requests) == 2


def test_databricks_catalog_uses_ready_chat_deployment_names(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", "test")
    monkeypatch.setenv("CORTEX_DATABRICKS_BASE_URL", "https://workspace.test/serving-endpoints")

    def handler(request):
        assert request.url.path == "/api/2.0/serving-endpoints"
        if "page_token" in request.url.params:
            assert request.url.params["page_token"] == "next"
            return httpx.Response(200, json={"endpoints": [{"name": "alias", "config": {
                "served_entities": [{"external_model": {"task": "llm/v1/chat"}}],
            }}]})
        return httpx.Response(200, json={"endpoints": [
            {"name": "chat", "task": "llm/v1/chat", "state": {"ready": "READY"}},
            {"name": "vector", "task": "llm/v1/embeddings"},
            {"name": "down", "task": "llm/v1/chat", "state": {"ready": "NOT_READY"}},
        ], "next_page_token": "next"})

    assert list_cloud_models("databricks", transport=httpx.MockTransport(handler)) == ["alias", "chat"]


def test_oci_requires_its_project_header(monkeypatch):
    provider = get_provider("oci")
    monkeypatch.delenv("OCI_GENAI_PROJECT_ID", raising=False)
    with pytest.raises(CloudProviderError, match="OCI_GENAI_PROJECT_ID"):
        enterprise_headers(provider)
    monkeypatch.setenv("OCI_GENAI_PROJECT_ID", "ocid1.generativeaiproject.example")
    assert enterprise_headers(provider) == {"OpenAI-Project": "ocid1.generativeaiproject.example"}


def test_named_gateways_use_separate_endpoints_keys_and_inventories(monkeypatch):
    from cortex_backend.services import cloud_catalog

    monkeypatch.setenv("CORTEX_CUSTOM_LAB_BASE_URL", "https://lab.example/v1")
    monkeypatch.setenv("CORTEX_CUSTOM_LAB_API_KEY", "test-lab")
    monkeypatch.setenv("CORTEX_CUSTOM_TEAM_BASE_URL", "http://localhost:9000/v1")
    monkeypatch.setenv("CORTEX_CUSTOM_TEAM_MODELS", "org/private:2")
    for spec in cloud_catalog._named_endpoints():
        monkeypatch.setitem(cloud_catalog.PROVIDERS, spec.id, spec)

    def list_lab(request):
        assert request.url == "https://lab.example/v1/models"
        assert request.headers["Authorization"] == "Bearer test-lab"
        return httpx.Response(200, json={"data": [{"id": "lab:2"}]})

    assert list_cloud_models("custom-lab", transport=httpx.MockTransport(list_lab)) == ["lab:2"]
    assert list_cloud_models("custom-team") == ["org/private:2"]
    assert cloud_catalog.connection(get_provider("custom-team")) == ("http://localhost:9000/v1", {})
