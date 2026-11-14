"""Amazon Bedrock Converse streaming and regional foundation/profile discovery."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
import os
from threading import Event
from time import monotonic_ns
from typing import Any
from urllib.parse import quote

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.eventstream import EventStreamBuffer, ParserError
from botocore.exceptions import BotoCoreError, ClientError
from botocore.session import Session
import httpx

from .cloud_transport import cloud_stream
from .cloud_types import CloudProviderError, image_mime_type


def bedrock_models(region: str) -> list[str]:
    try:
        with Session().create_client(
            "bedrock", region_name=region,
            config=Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 1}),
        ) as client:
            result = client.list_foundation_models(byOutputModality="TEXT")
            text_models = {
                item["modelId"]: item for item in result["modelSummaries"] if item.get("responseStreamingSupported")
            }
            names = [name for name, item in text_models.items() if "ON_DEMAND" in item.get("inferenceTypesSupported", ())]
            # Newer models may require inference profiles rather than their
            # foundation model IDs. The profile names, including their region
            # prefix, are exactly what Converse expects.
            try:
                pages = client.get_paginator("list_inference_profiles").paginate()
                for page in pages:
                    for profile in page["inferenceProfileSummaries"]:
                        if profile.get("status") != "ACTIVE":
                            continue
                        if any(model.get("modelArn", "").rsplit("/", 1)[-1] in text_models
                               for model in profile.get("models", ())):
                            names.append(profile["inferenceProfileId"])
            except ClientError as exc:
                if not names or exc.response.get("Error", {}).get("Code") != "AccessDeniedException":
                    raise
            return names
    except (BotoCoreError, ClientError, OSError):
        raise CloudProviderError("Amazon Bedrock could not list models. Check AWS credentials, region and "
                                 "listing permissions, or set CORTEX_BEDROCK_MODELS.") from None


def _bedrock_body(messages: list[dict], options: dict) -> dict[str, Any]:
    turns: list[dict] = []
    system: list[dict] = []
    for message in messages:
        role = message.get("role")
        text = message.get("content", "")
        if role == "system":
            if text:
                system.append({"text": text})
            continue
        if role not in ("user", "assistant"):
            raise CloudProviderError("Amazon Bedrock received an unsupported message role.")
        blocks: list[dict] = [{"text": text}] if text else []
        for image in message.get("images", ()):
            fmt = image_mime_type(image).removeprefix("image/")
            if fmt not in {"png", "jpeg", "gif", "webp"}:
                raise CloudProviderError("Amazon Bedrock supports PNG, JPEG, GIF and WebP images.")
            blocks.append({"image": {"format": fmt, "source": {"bytes": image}}})
        if blocks:
            turns.append({"role": role, "content": blocks})
    body: dict[str, Any] = {"messages": turns}
    if system:
        body["system"] = system
    inference = {}
    if isinstance(options.get("num_predict"), int) and options["num_predict"] > 0:
        inference["maxTokens"] = options["num_predict"]
    # Use model defaults: several newer Bedrock models reject sampling overrides.
    if inference:
        body["inferenceConfig"] = inference
    return body


def _bedrock_headers(url: str, data: bytes, region: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/vnd.amazon.eventstream"}
    if token := os.getenv("AWS_BEARER_TOKEN_BEDROCK"):
        return {**headers, "Authorization": f"Bearer {token}"}
    try:
        credentials = Session().get_credentials()
        if credentials is None:
            raise CloudProviderError("Set AWS credentials or AWS_BEARER_TOKEN_BEDROCK.")
        request = AWSRequest(method="POST", url=url, data=data, headers=headers)
        SigV4Auth(credentials.get_frozen_credentials(), "bedrock", region).add_auth(request)
        return dict(request.headers)
    except (BotoCoreError, ClientError, OSError):
        raise CloudProviderError("Amazon Bedrock could not load AWS credentials.") from None


def _bedrock_events(response: httpx.Response) -> Iterator[tuple[str, dict]]:
    # The SDK decoder verifies both frame checksums, validates lengths, and
    # handles partial frames, including those split across TCP packets.
    decoder = EventStreamBuffer()
    for chunk in response.iter_bytes():
        decoder.add_data(chunk)
        for event in decoder:
            if event.headers.get(":message-type") != "event":
                raise CloudProviderError("Amazon Bedrock reported a streaming error.")
            payload = json.loads(event.payload)
            if not isinstance(payload, dict):
                raise CloudProviderError("Amazon Bedrock returned an invalid stream event.")
            yield event.headers.get(":event-type", ""), payload


def converse_stream(
    base: str, region: str, *, model: str, messages: list[dict], options: dict,
    cancellation_event: Event | None, on_delta: Callable[[str, str], None] | None,
    client: httpx.Client,
) -> dict:
    if cancellation_event is not None and cancellation_event.is_set():
        return {"message": {"content": "", "thinking": None}, "done_reason": "cancelled"}
    if model in (".", ".."):
        raise CloudProviderError("Select a valid Amazon Bedrock model ID.")
    url = f"{base}/model/{quote(model, safe='')}/converse-stream"
    data = json.dumps(_bedrock_body(messages, options)).encode()
    headers = _bedrock_headers(url, data, region)
    content: list[str] = []
    thinking: list[str] = []
    reason = ""
    usage: dict = {}
    start = monotonic_ns()
    try:
        with cloud_stream(client, url, cancellation_event, headers=headers, content=data, follow_redirects=False) as response:
            if not response.is_success:
                raise CloudProviderError(f"Amazon Bedrock chat returned HTTP {response.status_code}. "
                                         "Check the model ID, region, credentials and invocation permissions.")
            for kind, item in _bedrock_events(response):
                if cancellation_event is not None and cancellation_event.is_set():
                    reason = "cancelled"
                    break
                if kind == "contentBlockDelta":
                    delta = item.get("delta", {})
                    thought = delta.get("reasoningContent", {}).get("text")
                    for part, value, parts in (("thinking", thought, thinking), ("content", delta.get("text"), content)):
                        if isinstance(value, str) and value:
                            parts.append(value)
                            if on_delta:
                                on_delta(part, value)
                elif kind == "messageStop":
                    reason = item.get("stopReason", "")
                    if reason in ("guardrail_intervened", "content_filtered", "malformed_model_output"):
                        raise CloudProviderError("Amazon Bedrock could not complete this response.")
                elif kind == "metadata":
                    usage = item.get("usage") or {}
    except (BotoCoreError, ParserError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
        if cancellation_event is not None and cancellation_event.is_set():
            reason = "cancelled"
        elif isinstance(exc, CloudProviderError):
            raise
        else:
            raise CloudProviderError("Amazon Bedrock could not finish the response.") from None
    if cancellation_event is not None and cancellation_event.is_set():
        reason = "cancelled"
    if not reason:
        raise CloudProviderError("Amazon Bedrock ended the stream before completing the response.")
    return {
        "done": True,
        "message": {"content": "".join(content), "thinking": "".join(thinking) or None},
        "prompt_eval_count": usage.get("inputTokens"), "eval_count": usage.get("outputTokens"),
        "done_reason": {"max_tokens": "length", "end_turn": "stop"}.get(reason, reason),
        "total_duration": monotonic_ns() - start,
    }
