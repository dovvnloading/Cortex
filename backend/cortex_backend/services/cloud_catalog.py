"""Provider presets and live, account-scoped chat model discovery."""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from .cloud_enterprise import ENTERPRISE_PROVIDERS, enterprise_headers, list_enterprise_models
from .cloud_platforms import PLATFORM_PROVIDERS, list_platform_models, platform_configured, resolve_platform
from .cloud_types import CloudProviderError, ProviderSpec

_SPECS = (
    ProviderSpec("openai", "OpenAI", "OPENAI_API_KEY", "https://api.openai.com/v1", protocol="responses"),
    ProviderSpec("anthropic", "Anthropic", "ANTHROPIC_API_KEY", "https://api.anthropic.com/v1", protocol="anthropic"),
    ProviderSpec("gemini", "Google Gemini", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta",
                 protocol="gemini"),
    ProviderSpec("xai", "xAI", "XAI_API_KEY", "https://api.x.ai/v1"),
    ProviderSpec("mistral", "Mistral AI", "MISTRAL_API_KEY", "https://api.mistral.ai/v1"),
    ProviderSpec("cohere", "Cohere", "COHERE_API_KEY", "https://api.cohere.ai/compatibility/v1"),
    ProviderSpec("deepseek", "DeepSeek", "DEEPSEEK_API_KEY", "https://api.deepseek.com"),
    ProviderSpec("moonshot", "Moonshot AI (Kimi)", "MOONSHOT_API_KEY", "https://api.moonshot.ai/v1"),
    ProviderSpec("minimax", "MiniMax", "MINIMAX_API_KEY", "https://api.minimax.io/v1", catalog=False),
    ProviderSpec("zai", "Z.ai", "ZAI_API_KEY", "https://api.z.ai/api/paas/v4"),
    ProviderSpec("qwen", "Alibaba Cloud Model Studio", "DASHSCOPE_API_KEY",
                 "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "CORTEX_QWEN_BASE_URL",
                 setup="Set DASHSCOPE_API_KEY. For a different region, set CORTEX_QWEN_BASE_URL to its compatible-mode/v1 URL."),
    ProviderSpec("baidu", "Baidu Qianfan", "QIANFAN_API_KEY", "https://qianfan.baidubce.com/v2"),
    ProviderSpec("volcengine", "ByteDance Volcano Ark", "ARK_API_KEY", "https://ark.cn-beijing.volces.com/api/v3"),
    ProviderSpec("perplexity", "Perplexity Orchestrator", "PERPLEXITY_API_KEY", "https://api.perplexity.ai/orchestrator/v1"),
    ProviderSpec("perplexity-agent", "Perplexity Agent API", "PERPLEXITY_API_KEY", "https://api.perplexity.ai/v1",
                 protocol="responses"),
    ProviderSpec("groq", "Groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1"),
    ProviderSpec("cerebras", "Cerebras", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1"),
    ProviderSpec("together", "Together AI", "TOGETHER_API_KEY", "https://api.together.ai/v1"),
    ProviderSpec("fireworks", "Fireworks AI", "FIREWORKS_API_KEY", "https://api.fireworks.ai/inference/v1"),
    ProviderSpec("nvidia", "NVIDIA", "NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1"),
    ProviderSpec("openrouter", "OpenRouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
    ProviderSpec("huggingface", "Hugging Face Inference Providers", "HF_TOKEN", "https://router.huggingface.co/v1"),
    ProviderSpec("deepinfra", "DeepInfra", "DEEPINFRA_TOKEN", "https://api.deepinfra.com/v1/openai"),
    ProviderSpec("vercel", "Vercel AI Gateway", "AI_GATEWAY_API_KEY", "https://ai-gateway.vercel.sh/v1"),
    ProviderSpec("custom", "Custom OpenAI-compatible endpoint", "CORTEX_CUSTOM_API_KEY", url_env="CORTEX_CUSTOM_BASE_URL",
                 setup="Set CORTEX_CUSTOM_BASE_URL to the API root, including any /v1. Set CORTEX_CUSTOM_API_KEY if required."),
    *PLATFORM_PROVIDERS,
    *ENTERPRISE_PROVIDERS,
)

def _named_endpoints() -> list[ProviderSpec]:
    """Each gateway has its own URL, key and optional deployment list."""
    providers = []
    for variable in sorted(os.environ):
        match = re.fullmatch(r"CORTEX_CUSTOM_([A-Z0-9][A-Z0-9_]{0,47})_BASE_URL", variable)
        if match is None or not os.environ[variable].strip():
            continue
        name = match.group(1)
        prefix = f"CORTEX_CUSTOM_{name}"
        providers.append(ProviderSpec(
            id="custom-" + name.lower().replace("_", "-"),
            label="Custom · " + name.replace("_", " "),
            key_env=prefix + "_API_KEY",
            url_env=variable,
            models_env=prefix + "_MODELS",
            setup=f"Set {variable} to the API root and {prefix}_API_KEY if required. "
                  f"Use {prefix}_MODELS for an explicit comma-separated model list.",
        ))
    return providers


# The launch environment is also the lifetime of credentials in the native app.
PROVIDERS = {provider.id: provider for provider in (*_SPECS, *_named_endpoints())}


def get_provider(provider_id: str) -> ProviderSpec:
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        raise CloudProviderError("Unknown cloud provider. Select a provider in Settings.") from None


def _env(name: str) -> str:
    return os.environ.get(name, "").strip() if name else ""


def provider_configured(provider_id: str) -> bool:
    """Check only local configuration. Loading Settings never initiates discovery."""
    spec = get_provider(provider_id)
    configured = platform_configured(spec)
    if configured is not None:
        return configured
    if not spec.catalog and not _env(spec.models_env):
        return False
    if spec.id == "oci" and not _env("OCI_GENAI_PROJECT_ID"):
        return False
    key_optional = spec.id == "custom" or spec.id.startswith("custom-")
    return bool((_env(spec.url_env) or spec.base_url) and (_env(spec.key_env) or key_optional))


def connection(spec: ProviderSpec) -> tuple[str, dict[str, str]]:
    resolved = resolve_platform(spec)
    if resolved is not None:
        return resolved
    if not provider_configured(spec.id):
        raise CloudProviderError(f"{spec.label} needs configuration. {spec.setup}")
    base = _env(spec.url_env) or spec.base_url
    try:
        url = httpx.URL(base)
    except httpx.InvalidURL:
        raise CloudProviderError("The configured provider API URL is invalid.") from None
    local = url.host in ("localhost", "127.0.0.1", "::1")
    if (not url.host or url.userinfo or url.query or url.fragment
            or (url.scheme != "https" and not (url.scheme == "http" and local))):
        raise CloudProviderError("Use an HTTPS API URL, or HTTP on loopback, without credentials, query or fragment.")
    key = _env(spec.key_env)
    if spec.protocol == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif spec.protocol == "gemini":
        headers = {"x-goog-api-key": key}
    else:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
    headers.update(enterprise_headers(spec))
    return str(url).rstrip("/"), headers


def raise_for_provider_status(response: httpx.Response, spec: ProviderSpec) -> None:
    if response.is_success:
        return
    status = response.status_code
    advice = {
        400: "Check that the model supports this request and its input types.",
        401: "Check the provider credentials.",
        403: "Check the account permissions for this model or catalog.",
        404: "Check the endpoint, deployment and model ID.",
        429: "Check provider quota or try again later.",
    }.get(status, "Check the provider status and configuration.")
    raise CloudProviderError(f"{spec.label} returned HTTP {status}. {advice}")


def valid_model_id(spec: ProviderSpec, value: object) -> bool:
    return (isinstance(value, str) and bool(value) and value.isprintable() and not value.isspace()
            and value == value.strip() and len(f"cloud:{spec.id}:{value}") <= 2048)


def _chat_id(spec: ProviderSpec, item: dict[str, Any]) -> str | None:
    model = item.get("id") or item.get("name")
    if not isinstance(model, str):
        return None
    if spec.protocol == "gemini":
        if "generateContent" not in (item.get("supportedGenerationMethods") or []):
            return None
        model = model.removeprefix("models/")
    if spec.id == "fireworks" and (not item.get("supportsServerless") or not item.get("conversationConfig")):
        return None
    if spec.id == "together" and item.get("type") not in (None, "chat"):
        return None
    if spec.id == "vercel" and item.get("type") not in (None, "language"):
        return None
    caps = item.get("capabilities") or {}
    if item.get("active") is False or (isinstance(caps, dict) and caps.get("completion_chat") is False):
        return None
    metadata = item.get("architecture") or item.get("modalities") or {}
    modalities = metadata.get("output_modalities") if isinstance(metadata, dict) else None
    modalities = modalities or item.get("output_modalities")
    if isinstance(modalities, list) and "text" not in modalities:
        return None
    # /models on several providers has no capability metadata. Exclude known
    # non-conversational families without freezing a whitelist of model releases.
    lower = model.lower()
    if spec.id == "meta" and any(name.startswith(("muse-image", "muse-video", "sam-")) or name == "sam"
                                 for name in lower.split("/")):
        return None
    if any(part in lower for part in ("embedding", "rerank", "whisper", "moderation", "transcribe", "realtime")):
        return None
    if lower.startswith(("text-embedding", "dall-e", "tts-", "babbage", "davinci", "gpt-image", "sora-", "imagen-", "veo-")):
        return None
    if spec.id in {"openai", "gemini"} and any(part in lower for part in ("-tts", "-audio", "-image", "-instruct")):
        return None
    # Multiple IBM gateway configurations can share the upstream model ID.
    # Their configured aliases address the individual routes at inference time.
    if spec.id == "watsonx" and isinstance(alias := item.get("alias"), str) and alias:
        return alias
    return model


def _dashscope_models(spec: ProviderSpec, http: httpx.Client, base: str, headers: dict[str, str]) -> list[str]:
    url = base.removesuffix("/compatible-mode/v1") + "/api/v1/models"
    models: list[str] = []
    for page in range(1, 201):
        response = http.get(url, headers=headers, params={"capabilities": "TG", "page_no": page, "page_size": 100})
        raise_for_provider_status(response, spec)
        data = response.json()
        output = data.get("output") or {}
        entries = output.get("models")
        total = output.get("total")
        if data.get("success") is False or not isinstance(entries, list) or not isinstance(total, int):
            raise CloudProviderError("Alibaba Cloud returned an invalid model catalog.")
        models.extend(item["model"] for item in entries if isinstance(item.get("model"), str))
        if page * int(output.get("page_size", 100)) >= total:
            return models
        if not entries:
            break
    raise CloudProviderError("Alibaba Cloud returned an incomplete model catalog.")


def _discover(spec: ProviderSpec, http: httpx.Client, base: str, headers: dict[str, str]) -> list[str]:
    if spec.id == "qwen":
        return _dashscope_models(spec, http, base, headers)
    url = f"{base}/models"
    params: dict[str, str] = {}
    if spec.id == "cohere":
        url, params = "https://api.cohere.ai/v1/models", {"endpoint": "chat", "page_size": "1000"}
    elif spec.id == "fireworks":
        url, params = "https://api.fireworks.ai/v1/accounts/fireworks/models", {"pageSize": "200"}
    elif spec.id == "xai":
        url = f"{base}/language-models"
    elif spec.id == "openrouter":
        url = f"{base}/models/user"
    elif spec.protocol == "gemini":
        params = {"pageSize": "1000"}
    elif spec.protocol == "anthropic":
        params = {"limit": "1000"}

    result: list[str] = []
    cursors: set[str] = set()
    while True:
        response = http.get(url, headers=headers, params=params)
        raise_for_provider_status(response, spec)
        payload = response.json()
        # Together returns the array directly. Other APIs wrap a page of
        # models in 'data' (OpenAI) or 'models' (Gemini, Cohere and xAI).
        if isinstance(payload, list):
            payload = {"data": payload}
        entries = next((payload[k] for k in ("data", "models") if k in payload), None)
        # Protobuf JSON omits an empty repeated field (for example a
        # Gemini key with no models), producing {} on the final page.
        if entries is None and payload == {} and spec.id in ("gemini", "fireworks"):
            entries = []
        if not isinstance(entries, list):
            raise CloudProviderError(f"{spec.label} returned an invalid model catalog.")
        for item in entries:
            if not isinstance(item, dict):
                raise CloudProviderError(f"{spec.label} returned an invalid model catalog.")
            model = _chat_id(spec, item)
            if model is not None:
                result.append(model)
        if payload.get("has_more"):
            key = "after_id" if spec.protocol == "anthropic" else "after"
            cursor = payload.get("last_id")
        elif payload.get("nextPageToken"):
            key, cursor = "pageToken", payload["nextPageToken"]
        elif payload.get("next_page_token"):
            key, cursor = "page_token", payload["next_page_token"]
        else:
            return result
        if not isinstance(cursor, str) or not cursor or cursor in cursors or len(cursors) >= 200:
            raise CloudProviderError(f"{spec.label} returned an invalid model catalog cursor.")
        cursors.add(cursor)
        params[key] = cursor


def list_cloud_models(provider_id: str, *, transport: httpx.BaseTransport | None = None) -> list[str]:
    spec = get_provider(provider_id)
    if not provider_configured(provider_id):
        raise CloudProviderError(f"{spec.label} needs configuration. {spec.setup}")
    manual = _env(spec.models_env)
    if manual:
        models = [value.strip() for value in manual.split(",") if value.strip()]
        if any(not valid_model_id(spec, value) for value in models):
            raise CloudProviderError(f"Check the model IDs in {spec.models_env}.")
    else:
        try:
            with httpx.Client(timeout=10.0, transport=transport, follow_redirects=False) as http:
                platform = list_platform_models(spec, http)
                if platform is not None:
                    models = platform
                else:
                    base, headers = connection(spec)
                    enterprise = list_enterprise_models(spec, http, base, headers)
                    if enterprise is not None:
                        models = enterprise
                    else:
                        models = _discover(spec, http, base, headers) if spec.catalog else []
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, IndexError, UnicodeError):
            raise CloudProviderError(f"Could not read the {spec.label} model catalog. Check the endpoint and credentials.") from None
    return sorted({model for model in models if valid_model_id(spec, model)}, key=str.casefold)
