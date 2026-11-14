"""Deployment-aware discovery and credentials for managed inference platforms."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import lru_cache
import os
import re
from threading import Event, Lock
from urllib.parse import quote

import google.auth
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
import httpx

from .cloud_bedrock import bedrock_models, converse_stream
from .cloud_types import CloudProviderError, ProviderSpec

PLATFORM_PROVIDERS = (
    ProviderSpec(
        "azure", "Microsoft Azure OpenAI / Foundry", "AZURE_OPENAI_API_KEY",
        url_env="AZURE_OPENAI_ENDPOINT", models_env="AZURE_OPENAI_DEPLOYMENTS",
        setup="Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN. "
              "Set AZURE_FOUNDRY_PROJECT to select a Foundry project, or list deployment names in AZURE_OPENAI_DEPLOYMENTS.",
    ),
    ProviderSpec(
        "vertex", "Google Cloud Vertex AI", "GOOGLE_CLOUD_ACCESS_TOKEN",
        setup="Set GOOGLE_CLOUD_PROJECT and configure Google Application Default Credentials or GOOGLE_CLOUD_ACCESS_TOKEN. "
              "GOOGLE_CLOUD_LOCATION defaults to global. Set CORTEX_VERTEX_MODELS for a manual list including partner model IDs.",
    ),
    ProviderSpec(
        "bedrock", "Amazon Bedrock", "AWS_BEARER_TOKEN_BEDROCK", protocol="bedrock",
        setup="Set AWS_REGION or AWS_DEFAULT_REGION and AWS_PROFILE (default for instance roles), AWS credentials, or a Bedrock API key. "
              "Grant model and inference-profile listing for discovery, or set CORTEX_BEDROCK_MODELS.",
    ),
    ProviderSpec(
        "bedrock-mantle", "Amazon Bedrock Mantle", "AWS_BEARER_TOKEN_BEDROCK",
        setup="Set AWS_REGION or AWS_DEFAULT_REGION and AWS_BEARER_TOKEN_BEDROCK to use the regional Mantle endpoint.",
    ),
)


def _region() -> str:
    return os.environ.get("AWS_REGION", "") or os.environ.get("AWS_DEFAULT_REGION", "")


def _location(value: str, variable: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value):
        raise CloudProviderError(f"Set {variable} to a valid region.")
    return value


def platform_configured(provider: ProviderSpec) -> bool | None:
    # Inspect only environment configuration. ADC and AWS role resolution can
    # contact identity services and are deferred until an actual provider call.
    if provider.id == "azure":
        return bool(os.getenv(provider.url_env) and (os.getenv(provider.key_env) or os.getenv("AZURE_OPENAI_AD_TOKEN")))
    if provider.id == "vertex":
        return bool(os.getenv("GOOGLE_CLOUD_PROJECT"))
    if provider.id == "bedrock":
        signals = ("AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID",
                   "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
        return bool(_region() and any(os.getenv(name) for name in signals))
    if provider.id == "bedrock-mantle":
        return bool(_region() and os.getenv(provider.key_env))
    return None


def _azure_resource() -> httpx.URL:
    try:
        url = httpx.URL(os.getenv("AZURE_OPENAI_ENDPOINT", ""))
    except httpx.InvalidURL:
        raise CloudProviderError("Set AZURE_OPENAI_ENDPOINT to the HTTPS resource endpoint.") from None
    if (url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment
            or url.path.rstrip("/") not in ("", "/openai/v1")):
        raise CloudProviderError("Set AZURE_OPENAI_ENDPOINT to the HTTPS resource endpoint.")
    return url.copy_with(path="")


@lru_cache(maxsize=1)
def _vertex_credentials():
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return credentials


_vertex_auth_lock = Lock()


def _vertex_headers(url: str) -> dict[str, str]:
    if token := os.getenv("GOOGLE_CLOUD_ACCESS_TOKEN"):
        return {"Authorization": f"Bearer {token}"}
    headers: dict[str, str] = {}
    try:
        with _vertex_auth_lock:
            credentials = _vertex_credentials()
            credentials.before_request(Request(), "POST", url, headers)
    except (GoogleAuthError, OSError, ValueError):
        raise CloudProviderError("Google Cloud authentication failed. Configure Application Default Credentials "
                                 "or set GOOGLE_CLOUD_ACCESS_TOKEN.") from None
    return headers


def resolve_platform(provider: ProviderSpec) -> tuple[str, dict[str, str]] | None:
    if provider.id == "azure":
        origin = str(_azure_resource()).rstrip("/")
        key = os.getenv(provider.key_env)
        if key:
            return f"{origin}/openai/v1", {"api-key": key}
        if token := os.getenv("AZURE_OPENAI_AD_TOKEN"):
            return f"{origin}/openai/v1", {"Authorization": f"Bearer {token}"}
        raise CloudProviderError(provider.setup)
    if provider.id == "vertex":
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]*", project):
            raise CloudProviderError("Set GOOGLE_CLOUD_PROJECT to your Google Cloud project ID or number.")
        location = _location(os.getenv("GOOGLE_CLOUD_LOCATION", "global"), "GOOGLE_CLOUD_LOCATION")
        prefix = "" if location == "global" else f"{location}-"
        url = (f"https://{prefix}aiplatform.googleapis.com/v1/projects/{quote(project, safe='')}"
               f"/locations/{location}/endpoints/openapi")
        return url, _vertex_headers(url)
    if provider.id in ("bedrock", "bedrock-mantle"):
        region = _location(_region(), "AWS_REGION")
        token = os.getenv(provider.key_env)
        if provider.id == "bedrock-mantle":
            if not token:
                raise CloudProviderError(provider.setup)
            return f"https://bedrock-mantle.{region}.api.aws/v1", {"Authorization": f"Bearer {token}"}
        suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
        return f"https://bedrock-runtime.{region}.{suffix}", ({"Authorization": f"Bearer {token}"} if token else {})
    return None


def platform_chat_base(provider: ProviderSpec, model: str, base: str) -> str:
    # Mantle advertises all models under /v1/models, including families served
    # through its separate /openai/v1 inference route.
    if provider.id == "bedrock-mantle" and (
        model.startswith("google.gemma") or (model.startswith("openai.gpt-") and not model.startswith("openai.gpt-oss"))
    ):
        return base.removesuffix("/v1") + "/openai/v1"
    return base


def _entries(client: httpx.Client, url: httpx.URL, headers: dict[str, str], *, vertex: bool) -> Iterator[dict]:
    field = "publisherModels" if vertex else "value"
    original = url
    seen: set[httpx.URL] = set()
    while url not in seen and len(seen) < 100:
        seen.add(url)
        response = client.get(url, headers=headers, follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get(field), list):
            raise CloudProviderError("The platform returned an invalid model catalog.")
        yield from (entry for entry in payload[field] if isinstance(entry, dict))
        continuation = payload.get("nextPageToken" if vertex else "nextLink")
        if not continuation:
            return
        if not isinstance(continuation, str):
            break
        if vertex:
            url = original.copy_set_param("pageToken", continuation)
        else:
            url = url.join(continuation)
            # Foundry's key or token must never follow a server-supplied URL
            # to a different resource, even through an HTTP redirect.
            if (url.scheme, url.netloc, url.path, url.userinfo) != (
                original.scheme, original.netloc, original.path, original.userinfo,
            ):
                break
    raise CloudProviderError("The platform returned an invalid catalog continuation.")


def _azure_deployments(client: httpx.Client, headers: dict[str, str]) -> list[str]:
    resource = _azure_resource()
    host = resource.host
    if host.endswith(".openai.azure.com"):
        host = host.removesuffix(".openai.azure.com") + ".services.ai.azure.com"
    project = os.getenv("AZURE_FOUNDRY_PROJECT", "_project")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", project):
        raise CloudProviderError("Set AZURE_FOUNDRY_PROJECT to the project name.")
    url = resource.copy_with(host=host, path=f"/api/projects/{project}/deployments").copy_merge_params(
        {"api-version": "v1", "deploymentType": "ModelDeployment"},
    )
    names: list[str] = []
    for entry in _entries(client, url, headers, vertex=False):
        caps = entry.get("capabilities") or {}
        if isinstance(caps, dict) and caps and str(caps.get("chatCompletion", "")).lower() != "true":
            # Keep rows with unknown capabilities (e.g. some third-party deployments)
            # unless the metadata affirmatively identifies a different operation.
            if "chatCompletion" in caps or any(str(caps.get(key)).lower() == "true" for key in (
                "embeddings", "imageGeneration", "audio", "completion",
            )):
                continue
        if isinstance(entry.get("name"), str) and entry["name"]:
            names.append(entry["name"])
    return names


def _vertex_models(client: httpx.Client, base: str, headers: dict[str, str]) -> list[str]:
    names: list[str] = []
    for publisher in ("google", "anthropic"):
        url = httpx.URL(base).copy_with(path=f"/v1beta1/publishers/{publisher}/models").copy_merge_params({"pageSize": 100})
        names.extend(_vertex_publisher_ids(_entries(client, url, headers, vertex=True)))
    return names


def _vertex_publisher_ids(entries: Iterator[dict]) -> Iterator[str]:
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        if name.startswith("publishers/anthropic/models/claude-"):
            yield "anthropic/" + name.rsplit("/", 1)[1]
        elif name.startswith("publishers/google/models/gemini-"):
            model = name.rsplit("/", 1)[1]
            if not any(tag in model for tag in ("embedding", "image", "audio", "live", "-tts", "robotics")):
                yield f"google/{model}"


def platform_messages_url(provider: ProviderSpec, model: str, base: str) -> str | None:
    """Two platform families expose Claude's native Messages wire format."""
    if provider.id == "bedrock-mantle" and model.startswith("anthropic.claude-"):
        return base.removesuffix("/v1") + "/anthropic/v1/messages"
    if provider.id == "vertex" and model.startswith("anthropic/claude-"):
        name = model.removeprefix("anthropic/")
        if not re.fullmatch(r"claude-[a-zA-Z0-9@._-]+", name):
            raise CloudProviderError("Use the Vertex publisher model ID, including its version when required.")
        return base.removesuffix("/endpoints/openapi") + f"/publishers/anthropic/models/{name}:streamRawPredict"
    return None


def list_platform_models(provider: ProviderSpec, client: httpx.Client) -> list[str] | None:
    if provider.id == "bedrock":
        return bedrock_models(_location(_region(), "AWS_REGION"))
    if provider.id not in ("azure", "vertex"):
        return None
    resolved = resolve_platform(provider)
    assert resolved is not None
    base, headers = resolved
    try:
        if provider.id == "azure":
            return _azure_deployments(client, headers)
        return _vertex_models(client, base, headers)
    except httpx.HTTPStatusError as exc:
        raise CloudProviderError(
            f"{provider.label} catalog returned HTTP {exc.response.status_code}. "
            f"Check provider access or set {provider.models_env} to your model or deployment IDs."
        ) from None
    except (httpx.HTTPError, ValueError):
        raise CloudProviderError(f"{provider.label} could not load its catalog.") from None

def bedrock_chat(
    provider: ProviderSpec, *, model: str, messages: list[dict], options: dict,
    cancellation_event: Event | None, on_delta: Callable[[str, str], None] | None, client: httpx.Client,
) -> dict:
    resolved = resolve_platform(provider)
    assert resolved is not None
    base, _ = resolved
    return converse_stream(base, _location(_region(), "AWS_REGION"), model=model, messages=messages, options=options,
                           cancellation_event=cancellation_event, on_delta=on_delta, client=client)
