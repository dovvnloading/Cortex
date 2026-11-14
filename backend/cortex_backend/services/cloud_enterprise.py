"""Hosted chat endpoints with account-specific URLs or deployment catalogs."""

from __future__ import annotations

import os
from typing import Any

import httpx

from .cloud_types import CloudProviderError, ProviderSpec


ENTERPRISE_PROVIDERS = (
    ProviderSpec("meta", "Meta Model API", "MODEL_API_KEY", "https://api.meta.ai/v1"),
    ProviderSpec("ai21", "AI21 Studio", "AI21_API_KEY", "https://api.ai21.com/studio/v1"),
    ProviderSpec("sambanova", "SambaNova", "SAMBANOVA_API_KEY", "https://api.sambanova.ai/v1"),
    ProviderSpec("digitalocean", "DigitalOcean Inference", "DIGITALOCEAN_INFERENCE_KEY", "https://inference.do-ai.run/v1"),
    ProviderSpec(
        "tencent", "Tencent Cloud TokenHub", "TOKENHUB_API_KEY", "https://tokenhub-intl.tencentmaas.com/v1",
        url_env="CORTEX_TENCENT_BASE_URL",
        setup="Set TOKENHUB_API_KEY. For the mainland China service, set CORTEX_TENCENT_BASE_URL to https://tokenhub.tencentmaas.com/v1.",
    ),
    ProviderSpec(
        "cloudflare", "Cloudflare Workers AI", "CLOUDFLARE_API_TOKEN", url_env="CORTEX_CLOUDFLARE_BASE_URL",
        setup="Set CLOUDFLARE_API_TOKEN and CORTEX_CLOUDFLARE_BASE_URL (https://api.cloudflare.com/client/v4/accounts/<account-id>/ai/v1).",
    ),
    ProviderSpec(
        "databricks", "Databricks Model Serving", "DATABRICKS_TOKEN", url_env="CORTEX_DATABRICKS_BASE_URL",
        setup="Set DATABRICKS_TOKEN and CORTEX_DATABRICKS_BASE_URL (https://<workspace-host>/serving-endpoints).",
    ),
    ProviderSpec(
        "snowflake", "Snowflake Cortex", "SNOWFLAKE_TOKEN", url_env="CORTEX_SNOWFLAKE_BASE_URL", catalog=False,
        setup="Set SNOWFLAKE_TOKEN, CORTEX_SNOWFLAKE_BASE_URL (https://<account-host>/api/v2/cortex/v1) and CORTEX_SNOWFLAKE_MODELS to your comma-separated model IDs.",
    ),
    ProviderSpec(
        "watsonx", "IBM watsonx.ai Model Gateway", "WATSONX_IAM_TOKEN", url_env="CORTEX_WATSONX_BASE_URL",
        setup="Set WATSONX_IAM_TOKEN and CORTEX_WATSONX_BASE_URL (https://<region>.ml.cloud.ibm.com/ml/gateway/v1). WATSONX_PROJECT_ID is optional.",
    ),
    ProviderSpec(
        "oci", "Oracle Cloud Generative AI", "OCI_GENAI_API_KEY", url_env="CORTEX_OCI_BASE_URL", catalog=False,
        setup="Set OCI_GENAI_API_KEY, OCI_GENAI_PROJECT_ID, CORTEX_OCI_BASE_URL (https://inference.generativeai.<region>.oci.oraclecloud.com/openai/v1) and comma-separated CORTEX_OCI_MODELS.",
    ),
)


def enterprise_headers(provider: ProviderSpec) -> dict[str, str]:
    if provider.id == "oci":
        project = os.getenv("OCI_GENAI_PROJECT_ID", "").strip()
        if not project:
            raise CloudProviderError("Set OCI_GENAI_PROJECT_ID to the OCID of your Generative AI project.")
        return {"OpenAI-Project": project}
    if provider.id == "watsonx" and (watsonx_project := os.getenv("WATSONX_PROJECT_ID")):
        return {"X-IBM-Project-Id": watsonx_project}
    return {}


def _rows(data: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise CloudProviderError("The provider returned an unreadable model catalog.")
    return [row for row in data[key] if isinstance(row, dict)]


def _databricks_chat(endpoint: dict[str, Any]) -> bool:
    if (endpoint.get("state") or {}).get("ready") == "NOT_READY":
        return False
    if endpoint.get("task") == "llm/v1/chat":
        return True
    entities = (endpoint.get("config") or {}).get("served_entities") or []
    return any(
        isinstance(entity, dict) and (entity.get("external_model") or {}).get("task") == "llm/v1/chat"
        for entity in entities
    )


def list_enterprise_models(
    provider: ProviderSpec, client: httpx.Client, base_url: str, headers: dict[str, str],
) -> list[str] | None:
    if provider.id == "cloudflare":
        url = base_url.rstrip("/").removesuffix("/v1") + "/models/search"
        names: list[str] = []
        for page in range(1, 101):
            response = client.get(url, headers=headers, params={"task": "Text Generation", "page": page, "per_page": 100})
            response.raise_for_status()
            data = response.json()
            entries = _rows(data, "result")
            names.extend(item["name"] for item in entries if isinstance(item.get("name"), str))
            total = (data.get("result_info") or {}).get("total_pages")
            if (isinstance(total, int) and page >= total) or (total is None and len(entries) < 100):
                return names
        raise CloudProviderError("The Cloudflare catalog exceeded the page limit.")
    if provider.id == "databricks":
        url = base_url.rstrip("/").removesuffix("/serving-endpoints") + "/api/2.0/serving-endpoints"
        names = []
        params: dict[str, str] = {}
        seen: set[str] = set()
        for _ in range(100):
            response = client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            names.extend(
                item["name"] for item in _rows(data, "endpoints")
                if isinstance(item.get("name"), str) and _databricks_chat(item)
            )
            token = data.get("next_page_token")
            if not token:
                return names
            if not isinstance(token, str) or token in seen:
                break
            seen.add(token)
            params = {"page_token": token}
        raise CloudProviderError("Databricks returned an incomplete model catalog.")
    return None
