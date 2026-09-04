"""Chat threads, groups, messages, forks, and regenerations.

Registered by cortex_backend.api.routers.build_router. Shared helpers live
in cortex_backend.api.routes.
"""

from __future__ import annotations

from fastapi import APIRouter
from cortex_backend.api.app_types import BackendDependenciesProtocol
from cortex_backend.api.jobs import (
    JobConflict,
    JobRegistryClosed,
)
from cortex_backend.api.routes import (
    _accepted,
    _chat_response,
    _durable_owner,
    _raise_chat_attachment_error,
    _raise_repository_error,
    _reject_invalid_new_chat_thread_id,
    _request_fingerprint,
    _start_generation_job,
    _validate_chat_attachment_refs,
)
from cortex_backend.api.schemas import (
    AddMessageRequest,
    ChatGroup,
    ChatResponse,
    ChatSummary,
    CreateChatGroupRequest,
    CreateChatRequest,
    ForkRequest,
    GenerationRequest,
    JobAccepted,
    MoveChatToGroupRequest,
    RegenerationRequest,
    RenameChatRequest,
    UpdateChatGroupRequest,
)
from cortex_backend.api.security import SessionPrincipal
from cortex_backend.repositories.chats import (
    ChatGroupNotFound,
    ChatRepositoryError,
    ChatRevisionConflict,
)
from cortex_backend.services.attachments import ChatAttachmentError
from cortex_backend.services.chat import (
    ChatDomainError,
    chat_revision,
    message_position,
)
from fastapi import (
    Depends,
    HTTPException,
    Request,
    status,
)
from uuid import uuid4


def register(router: APIRouter, *, require_session, dependencies) -> None:
    """Attach the chats routes to ``router``."""


    @router.get("/chats", response_model=list[ChatSummary])
    def list_chats(
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> list[ChatSummary]:
        try:
            return [
                ChatSummary.model_validate(item) for item in deps.chats.list_summaries()
            ]
        except Exception as exc:
            _raise_repository_error("list chats", exc)


    @router.get("/chats/{thread_id}", response_model=ChatResponse)
    def get_chat(
        thread_id: str,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatResponse:
        try:
            chat = deps.chats.get_chat(thread_id)
        except Exception as exc:
            _raise_repository_error("load chat", exc)
        if chat is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found."
            )
        return _chat_response(chat)


    @router.post(
        "/chats", response_model=ChatResponse, status_code=status.HTTP_201_CREATED
    )
    def create_chat(
        payload: CreateChatRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatResponse:
        thread_id = uuid4().hex
        try:
            deps.chats.create_chat(thread_id, payload.title.strip())
            chat = deps.chats.get_chat(thread_id)
        except Exception as exc:
            _raise_repository_error("create chat", exc)
        if chat is None:
            raise HTTPException(
                status_code=500, detail="Chat creation did not persist."
            )
        return _chat_response(chat)


    @router.patch("/chats/{thread_id}", response_model=ChatResponse)
    def rename_chat(
        thread_id: str,
        payload: RenameChatRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatResponse:
        try:
            if deps.chats.get_chat(thread_id) is None:
                raise HTTPException(status_code=404, detail="Chat not found.")
            deps.chats.rename_chat(thread_id, payload.title.strip())
            chat = deps.chats.get_chat(thread_id)
        except HTTPException:
            raise
        except Exception as exc:
            _raise_repository_error("rename chat", exc)
        if chat is None:
            raise HTTPException(status_code=500, detail="Chat rename did not persist.")
        return _chat_response(chat)


    @router.delete("/chats/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_chat(
        thread_id: str,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> None:
        try:
            deps.chats.delete_chat(thread_id)
        except Exception as exc:
            _raise_repository_error("delete chat", exc)


    @router.get("/chat-groups", response_model=list[ChatGroup])
    def list_chat_groups(
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> list[ChatGroup]:
        try:
            return [ChatGroup.model_validate(item) for item in deps.chats.list_groups()]
        except Exception as exc:
            _raise_repository_error("list chat groups", exc)


    @router.post(
        "/chat-groups", response_model=ChatGroup, status_code=status.HTTP_201_CREATED
    )
    def create_chat_group(
        payload: CreateChatGroupRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatGroup:
        group_id = uuid4().hex
        try:
            deps.chats.create_group(group_id, payload.name.strip())
            created = next(
                (item for item in deps.chats.list_groups() if item["id"] == group_id),
                None,
            )
        except Exception as exc:
            _raise_repository_error("create chat group", exc)
        if created is None:
            raise HTTPException(
                status_code=500, detail="Chat group creation did not persist."
            )
        return ChatGroup.model_validate(created)


    @router.patch("/chat-groups/{group_id}", response_model=ChatGroup)
    def update_chat_group(
        group_id: str,
        payload: UpdateChatGroupRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatGroup:
        try:
            updated = deps.chats.update_group(
                group_id,
                name=payload.name.strip() if payload.name is not None else None,
                collapsed=payload.collapsed,
            )
            if not updated:
                raise HTTPException(status_code=404, detail="Chat group not found.")
            group = next(
                (item for item in deps.chats.list_groups() if item["id"] == group_id),
                None,
            )
        except HTTPException:
            raise
        except Exception as exc:
            _raise_repository_error("update chat group", exc)
        if group is None:
            raise HTTPException(status_code=404, detail="Chat group not found.")
        return ChatGroup.model_validate(group)


    @router.delete("/chat-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_chat_group(
        group_id: str,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> None:
        """Delete the group only. Its chats return to the ungrouped list --
        tidying the sidebar must never destroy conversations."""
        try:
            deps.chats.delete_group(group_id)
        except Exception as exc:
            _raise_repository_error("delete chat group", exc)


    @router.patch("/chats/{thread_id}/group", response_model=ChatSummary)
    def move_chat_to_group(
        thread_id: str,
        payload: MoveChatToGroupRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatSummary:
        try:
            moved = deps.chats.set_chat_group(thread_id, payload.group_id)
            if not moved:
                raise HTTPException(status_code=404, detail="Chat not found.")
            summary = next(
                (item for item in deps.chats.list_summaries() if item["id"] == thread_id),
                None,
            )
        except HTTPException:
            raise
        except ChatGroupNotFound as exc:
            raise HTTPException(status_code=404, detail="Chat group not found.") from exc
        except Exception as exc:
            _raise_repository_error("move chat", exc)
        if summary is None:
            raise HTTPException(status_code=404, detail="Chat not found.")
        return ChatSummary.model_validate(summary)


    @router.post("/chats/{thread_id}/messages", response_model=ChatResponse)
    def add_message(
        thread_id: str,
        payload: AddMessageRequest,
        request: Request,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        principal: SessionPrincipal = Depends(require_session),
    ) -> ChatResponse:
        try:
            existing = deps.chats.get_chat(thread_id)
            if existing is None:
                _reject_invalid_new_chat_thread_id(thread_id)
            attachment_refs = _validate_chat_attachment_refs(
                request,
                deps,
                principal,
                payload.attachments or [],
            )
            deps.chats.add_message(
                thread_id,
                payload.role,
                payload.content,
                sources=payload.sources,
                thoughts=payload.thoughts,
                attachments=[attachment.model_dump(mode="json") for attachment in attachment_refs],
                thread_title="New Chat" if existing is None else None,
                expected_revision=payload.base_revision,
            )
            chat = deps.chats.get_chat(thread_id)
        except ChatAttachmentError as exc:
            _raise_chat_attachment_error(exc)
        except ChatRevisionConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ChatDomainError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except Exception as exc:
            _raise_repository_error("save message", exc)
        if chat is None:
            raise HTTPException(status_code=500, detail="Message did not persist.")
        return _chat_response(chat)


    @router.post(
        "/chats/{thread_id}/forks",
        response_model=ChatResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def fork_chat(
        thread_id: str,
        payload: ForkRequest,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        _: SessionPrincipal = Depends(require_session),
    ) -> ChatResponse:
        try:
            source = deps.chats.get_chat(thread_id)
            if source is None:
                raise HTTPException(status_code=404, detail="Chat not found.")
            message_position(source, payload.message_id)
            new_thread_id = uuid4().hex
            deps.chats.fork_chat(thread_id, payload.message_id, new_thread_id)
            forked = deps.chats.get_chat(new_thread_id)
        except HTTPException:
            raise
        except (ChatDomainError, ChatRepositoryError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            _raise_repository_error("fork chat", exc)
        if forked is None:
            raise HTTPException(status_code=500, detail="Chat fork did not persist.")
        return _chat_response(forked)


    @router.post(
        "/chats/{thread_id}/regenerations",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def regenerate_chat(
        thread_id: str,
        payload: RegenerationRequest,
        request: Request,
        deps: BackendDependenciesProtocol = Depends(dependencies),
        principal: SessionPrincipal = Depends(require_session),
    ) -> JobAccepted:
        request_fingerprint = _request_fingerprint(
            "regenerate",
            payload,
            path_thread_id=thread_id,
        )
        try:
            reservation = request.app.state.jobs.reserve(
                kind="generation",
                owner=_durable_owner(principal),
                thread_id=thread_id,
                request_id=payload.request_id,
                request_fingerprint=request_fingerprint,
            )
            if not reservation.created:
                snapshot, _ = await request.app.state.jobs.wait_until_prepared(
                    reservation.snapshot.job_id,
                    owner=_durable_owner(principal),
                )
            else:
                try:
                    chat = deps.chats.get_chat(thread_id)
                    if chat is None:
                        raise HTTPException(status_code=404, detail="Chat not found.")
                    position = message_position(chat, payload.message_id)
                    messages = list(chat.get("messages", ()))
                    if position != len(messages) - 1:
                        raise ChatDomainError(
                            "Only the final message can be regenerated."
                        )
                    target_role = messages[position].get("role")
                    if target_role == "assistant":
                        if (
                            position == 0
                            or messages[position - 1].get("role") != "user"
                        ):
                            raise ChatDomainError(
                                "The selected response has no user turn to regenerate."
                            )
                    elif target_role != "user":
                        raise ChatDomainError(
                            "Only an assistant response or an unanswered message can be regenerated."
                        )
                    # A dangling user turn (a prior attempt admitted this
                    # message, then failed before any reply was persisted) is
                    # itself the current turn to answer, so its own content
                    # -- not the message before it -- is the model's input.
                    user_input = (
                        payload.user_input
                        or messages[position - (1 if target_role == "assistant" else 0)].get("content", "")
                    ).strip()
                    if not user_input:
                        raise ChatDomainError(
                            "A regeneration request needs user input."
                        )
                    current_revision = chat_revision(chat)
                    if (
                        payload.base_revision is not None
                        and payload.base_revision != current_revision
                    ):
                        raise ChatDomainError(
                            "This chat changed. Reload it before regenerating."
                        )
                    generation_payload = GenerationRequest(
                        request_id=payload.request_id,
                        thread_id=thread_id,
                        user_input=user_input,
                        base_revision=current_revision,
                        attachments=payload.attachments,
                        options=payload.options,
                    )
                    snapshot, _ = await _start_generation_job(
                        request,
                        deps,
                        principal,
                        generation_payload,
                        request_fingerprint=request_fingerprint,
                        reservation=reservation,
                        target_message_id=payload.message_id,
                        history_messages=messages[:position],
                    )
                finally:
                    request.app.state.jobs.abort_reservation(
                        reservation,
                        owner=_durable_owner(principal),
                    )
        except HTTPException:
            raise
        except JobRegistryClosed as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except JobConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ChatRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ChatDomainError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _accepted(snapshot)
