"""Authenticated native-broker loop for the fixed recipe worker.

The package entrypoint owns only this loop.  It accepts commands after the native
broker has authenticated the peer and the launcher has bound the exact job,
principal, PID, and AppContainer token.  Every command is decoded into the
transport-neutral worker protocol; no command line, path, shell, or host-process
fallback is ever interpreted here.
"""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
import re
from threading import Event, Thread
from time import monotonic
from typing import Any, Callable, Final, Literal, Protocol

from .broker import BrokerMessage
from .lifecycle import RuntimeHealth
from .recipe_provider import RecipeImageProvider
from .worker_protocol import (
    RecipeWorkerDispatcher,
    RecipeWorkerSession,
    WorkerAck,
    WorkerCancel,
    WorkerError,
    WorkerCollect,
    WorkerInputChunk,
    WorkerInputComplete,
    WorkerOperation,
    WorkerOutputChunk,
    WorkerPrepare,
    WorkerProtocolError,
    WorkerResult,
)


_PRINCIPAL = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DEFAULT_MAX_MESSAGES: Final[int] = 16_384
DEFAULT_WATCHDOG_TIMEOUT_MS: Final[int] = 30_000


class WorkerRuntimeError(ValueError):
    """Stable, redacted worker-loop failure category."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid worker runtime code")
        self.code = code
        super().__init__("The recipe worker runtime stopped safely.")


class WorkerProvider(Protocol):
    def start(self, sandbox_health: RuntimeHealth) -> RuntimeHealth:
        """Enable the fixed provider after the authenticated native boundary."""

    def stop(self) -> RuntimeHealth:
        """Release provider state before the worker exits."""

    def transform(self, plan: Any, content: bytes, *, cancel_check: Callable[[], bool]) -> Any:
        """Transform only validated in-memory bytes."""


class WorkerBrokerConnection(Protocol):
    """The narrow authenticated transport surface consumed by the worker loop."""

    def receive_message(self) -> BrokerMessage:
        """Receive one already-authenticated broker message."""

    def send_message(self, message: BrokerMessage) -> None:
        """Send one broker response using the transport's authenticated direction."""

    def close(self) -> None:
        """Close the connection and release native handles."""


@dataclass(frozen=True, slots=True)
class WorkerRuntimeReport:
    """Non-sensitive terminal state for package probes and tests."""

    processed_messages: int
    terminal_state: Literal["complete", "cancelled", "failed", "message_budget"]


def _validate_principal(value: str) -> str:
    if _PRINCIPAL.fullmatch(value) is None:
        raise ValueError("worker principal is invalid")
    return value


def _validate_id(value: str, field: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"worker {field} is invalid")
    return value


class RecipeWorkerBrokerRuntime:
    """Run one bounded worker request over an authenticated broker connection."""

    _MODELS: dict[str, type[Any]] = {
        "prepare": WorkerPrepare,
        "input_chunk": WorkerInputChunk,
        "input_complete": WorkerInputComplete,
        "cancel": WorkerCancel,
        "collect": WorkerCollect,
    }

    def __init__(
        self,
        connection: WorkerBrokerConnection,
        *,
        expected_principal_id: str,
        job_id: str,
        provider_factory: Callable[[], WorkerProvider] = RecipeImageProvider,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        watchdog_timeout_ms: int = DEFAULT_WATCHDOG_TIMEOUT_MS,
    ) -> None:
        if not all(
            callable(getattr(connection, method, None))
            for method in ("receive_message", "send_message", "close")
        ):
            raise TypeError("worker runtime requires an authenticated broker connection")
        self._connection = connection
        self._principal = _validate_principal(expected_principal_id)
        self._job_id = _validate_id(job_id, "job ID")
        if not callable(provider_factory):
            raise TypeError("worker provider factory must be callable")
        if type(max_messages) is not int or not 1 <= max_messages <= DEFAULT_MAX_MESSAGES:
            raise ValueError("worker message budget is invalid")
        if type(watchdog_timeout_ms) is not int or not 1 <= watchdog_timeout_ms <= 600_000:
            raise ValueError("worker watchdog timeout is invalid")
        self._provider_factory = provider_factory
        self._max_messages = max_messages
        self._watchdog_timeout_ms = watchdog_timeout_ms

    @staticmethod
    def _envelope_body(message: BrokerMessage) -> Any:
        model_type = RecipeWorkerBrokerRuntime._MODELS.get(message.operation)
        if model_type is None:
            raise WorkerProtocolError("operation_invalid")
        try:
            model = model_type.model_validate(message.body)
        except (TypeError, ValueError):
            raise WorkerProtocolError("message_invalid") from None
        if model.request_id != message.request_id or model.job_id != message.job_id:
            raise WorkerProtocolError("request_identity_mismatch")
        return model

    def _validate_envelope(self, message: BrokerMessage) -> None:
        if message.direction != "to_executor":
            raise WorkerRuntimeError("broker_direction_invalid")
        if message.installation_principal_id != self._principal:
            raise WorkerRuntimeError("broker_principal_mismatch")
        if message.job_id != self._job_id:
            raise WorkerRuntimeError("broker_job_mismatch")
        self._envelope_body(message)

    def _response(
        self,
        request: BrokerMessage,
        body: WorkerAck | WorkerError | WorkerOutputChunk | WorkerResult,
    ) -> BrokerMessage:
        return BrokerMessage(
            schema_version="broker.message.v1",
            direction="to_broker",
            operation=request.operation,
            request_id=request.request_id,
            job_id=request.job_id,
            installation_principal_id=self._principal,
            body=body.model_dump(mode="json"),
        )

    def _dispatch(
        self,
        dispatcher: RecipeWorkerDispatcher,
        request: BrokerMessage,
    ) -> WorkerAck | WorkerOutputChunk | WorkerResult | None:
        self._validate_envelope(request)
        result = dispatcher.dispatch(request.operation, request.body)
        if result is not None:
            return result
        if request.operation == "cancel":
            acknowledged: WorkerOperation = "cancel"
        else:
            acknowledged = request.operation  # type: ignore[assignment]
        return WorkerAck(
            schema_version="recipe.worker.ack.v1",
            request_id=request.request_id,
            job_id=request.job_id,
            acknowledged_operation=acknowledged,
        )

    @staticmethod
    def _receive_loop(
        connection: WorkerBrokerConnection,
        incoming: Queue[Any],
        stop_event: Event,
    ) -> None:
        """Keep broker reads live while a provider transform is running."""

        while not stop_event.is_set():
            try:
                incoming.put(connection.receive_message())
            except Exception as error:
                if not stop_event.is_set():
                    incoming.put(error)
                return

    def _dispatch_completion(
        self,
        dispatcher: RecipeWorkerDispatcher,
        request: BrokerMessage,
        completion: Queue[Any],
    ) -> None:
        try:
            completion.put((request, self._dispatch(dispatcher, request), None))
        except WorkerRuntimeError as error:
            completion.put((request, None, error))
        except WorkerProtocolError as error:
            completion.put((request, None, error))
        except Exception:
            completion.put((request, None, WorkerProtocolError("provider_failed")))

    def run(self) -> WorkerRuntimeReport:
        """Serve one authenticated request and close transport/provider state.

        Broker reads run on a dedicated daemon thread so a cancellation frame can
        reach the session while Pillow is decoding or encoding.  The transform
        itself is also isolated to a daemon thread; the native Job Object remains
        the final watchdog if a provider ignores its cancellation callback.
        """

        provider: WorkerProvider | None = None
        processed = 0
        terminal: Literal["complete", "cancelled", "failed", "message_budget"] = "failed"
        receiver_stop = Event()
        incoming: Queue[Any] = Queue(maxsize=8)
        completion: Queue[Any] = Queue(maxsize=1)
        receiver: Thread | None = None
        completion_thread: Thread | None = None
        try:
            try:
                provider = self._provider_factory()
                health = provider.start(
                    RuntimeHealth.ready("Native broker identity and job binding established.")
                )
            except WorkerRuntimeError:
                raise
            except Exception:
                raise WorkerRuntimeError("provider_start_failed") from None
            if not isinstance(health, RuntimeHealth) or not health.available:
                raise WorkerRuntimeError("provider_unavailable")
            dispatcher = RecipeWorkerDispatcher(RecipeWorkerSession(provider))
            receiver = Thread(
                target=self._receive_loop,
                args=(self._connection, incoming, receiver_stop),
                name="cortex-worker-broker-reader",
                daemon=True,
            )
            receiver.start()
            pending_collect: BrokerMessage | None = None
            cancel_acknowledged = False
            active_request: BrokerMessage | None = None
            transform_started_at = 0.0

            while processed < self._max_messages:
                if (
                    active_request is not None
                    and completion_thread is not None
                    and completion_thread.is_alive()
                    and not cancel_acknowledged
                    and monotonic() - transform_started_at
                    > self._watchdog_timeout_ms / 1000
                ):
                    timeout_cancel = BrokerMessage(
                        schema_version="broker.message.v1",
                        direction="to_executor",
                        operation="cancel",
                        request_id=active_request.request_id,
                        job_id=active_request.job_id,
                        installation_principal_id=self._principal,
                        body={
                            "schema_version": "recipe.worker.cancel.v1",
                            "request_id": active_request.request_id,
                            "job_id": active_request.job_id,
                            "reason": "timeout",
                        },
                    )
                    try:
                        dispatcher.dispatch("cancel", timeout_cancel.body)
                    except WorkerProtocolError:
                        pass
                    self._connection.send_message(
                        self._response(
                            active_request,
                            WorkerError(
                                schema_version="recipe.worker.error.v1",
                                request_id=active_request.request_id,
                                job_id=active_request.job_id,
                                code="timeout",
                            ),
                        )
                    )
                    terminal = "failed"
                    return WorkerRuntimeReport(processed, terminal)
                try:
                    request = incoming.get(timeout=0.05)
                except Empty:
                    try:
                        completed_request, response, error = completion.get_nowait()
                    except Empty:
                        continue
                    completion_thread = None
                    active_request = None
                    if isinstance(error, WorkerRuntimeError):
                        raise error
                    if isinstance(error, WorkerProtocolError):
                        if error.code == "cancelled" and cancel_acknowledged:
                            terminal = "cancelled"
                            return WorkerRuntimeReport(processed, terminal)
                        response = WorkerError(
                            schema_version="recipe.worker.error.v1",
                            request_id=completed_request.request_id,
                            job_id=completed_request.job_id,
                            code=error.code,
                        )
                        self._connection.send_message(
                            self._response(completed_request, response)
                        )
                        terminal = "cancelled" if error.code == "cancelled" else "failed"
                        return WorkerRuntimeReport(processed, terminal)
                    if response is not None:
                        self._connection.send_message(
                            self._response(completed_request, response)
                        )
                    if pending_collect is not None:
                        deferred = pending_collect
                        pending_collect = None
                        try:
                            deferred_response = self._dispatch(dispatcher, deferred)
                        except WorkerProtocolError as deferred_error:
                            deferred_response = WorkerError(
                                schema_version="recipe.worker.error.v1",
                                request_id=deferred.request_id,
                                job_id=deferred.job_id,
                                code=deferred_error.code,
                            )
                        if deferred_response is not None:
                            self._connection.send_message(
                                self._response(deferred, deferred_response)
                            )
                        if isinstance(deferred_response, WorkerOutputChunk):
                            if deferred_response.final:
                                terminal = "complete"
                                return WorkerRuntimeReport(processed, terminal)
                    if cancel_acknowledged:
                        terminal = "cancelled"
                        return WorkerRuntimeReport(processed, terminal)
                    continue
                if isinstance(request, Exception):
                    raise WorkerRuntimeError("broker_receive_failed") from None
                processed += 1
                if not isinstance(request, BrokerMessage):
                    raise WorkerRuntimeError("broker_message_invalid")
                if request.operation == "input_complete" and completion_thread is not None:
                    try:
                        response = self._dispatch(dispatcher, request)
                    except WorkerProtocolError as error:
                        response = WorkerError(
                            schema_version="recipe.worker.error.v1",
                            request_id=request.request_id,
                            job_id=request.job_id,
                            code=error.code,
                        )
                    if response is not None:
                        self._connection.send_message(self._response(request, response))
                    continue
                if request.operation == "collect" and completion_thread is not None:
                    if pending_collect is None:
                        pending_collect = request
                    else:
                        self._connection.send_message(
                            self._response(
                                request,
                                WorkerError(
                                    schema_version="recipe.worker.error.v1",
                                    request_id=request.request_id,
                                    job_id=request.job_id,
                                    code="request_state_invalid",
                                ),
                            )
                        )
                    continue
                if request.operation == "input_complete":
                    self._validate_envelope(request)
                    active_request = request
                    transform_started_at = monotonic()
                    completion_thread = Thread(
                        target=self._dispatch_completion,
                        args=(dispatcher, request, completion),
                        name="cortex-worker-transform",
                        daemon=True,
                    )
                    completion_thread.start()
                    continue
                try:
                    response = self._dispatch(dispatcher, request)
                except WorkerRuntimeError:
                    raise
                except WorkerProtocolError as error:
                    response = WorkerError(
                        schema_version="recipe.worker.error.v1",
                        request_id=request.request_id,
                        job_id=request.job_id,
                        code=error.code,
                    )
                if response is not None:
                    self._connection.send_message(self._response(request, response))
                session_state = dispatcher.state
                if request.operation == "cancel":
                    if isinstance(response, WorkerAck):
                        cancel_acknowledged = True
                        if completion_thread is None:
                            terminal = "cancelled"
                            return WorkerRuntimeReport(processed, terminal)
                    elif isinstance(response, WorkerError):
                        cancel_acknowledged = False
                if session_state == "failed":
                    terminal = "failed"
                    return WorkerRuntimeReport(processed, terminal)
                if request.operation == "collect" and isinstance(response, WorkerOutputChunk):
                    if response.final:
                        terminal = "complete"
                        return WorkerRuntimeReport(processed, terminal)

            terminal = "message_budget"
            raise WorkerRuntimeError("message_budget_exceeded")
        finally:
            receiver_stop.set()
            if provider is not None:
                try:
                    provider.stop()
                except Exception:
                    pass
            self._connection.close()


__all__ = [
    "DEFAULT_MAX_MESSAGES",
    "DEFAULT_WATCHDOG_TIMEOUT_MS",
    "RecipeWorkerBrokerRuntime",
    "WorkerRuntimeError",
    "WorkerRuntimeReport",
]
