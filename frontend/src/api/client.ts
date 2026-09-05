import type {
  AddMemoryRequest,
  AttachmentStageAccepted,
  AttachmentStageRequest,
  ChatAttachment,
  ChatAttachmentStageRequest,
  ChatGroup,
  ChatResponse,
  ChatSummary,
  CodeExecutionAccepted,
  CodeExecutionRequest,
  CodeExecutionSourceResponse,
  CreateChatGroupRequest,
  CreateChatRequest,
  DiagnosticsResponse,
  MoveChatToGroupRequest,
  UpdateChatGroupRequest,
  ExecutionSSEEvent,
  ExecutionApprovalDecisionRequest,
  ExecutionStatusResponse,
  ExecutionTaskListResponse,
  RecipeImageTransformAccepted,
  RecipeImageTransformRequest,
  ScratchComputeAccepted,
  ScratchComputeRequest,
  ForkRequest,
  GenerationEvent,
  GenerationRequest,
  ShutdownResponse,
  HuggingFaceFileListResponse,
  JobAccepted,
  JobStatusResponse,
  HealthResponse,
  HandoffResponse,
  MemoryResponse,
  ModelDownloadRequest,
  ModelPullRequest,
  ModelResponse,
  RegenerationRequest,
  RenameChatRequest,
  SessionExchangeResponse,
  SettingsResponse,
  SettingsUpdateRequest,
  SystemResponse,
  SSEEvent,
} from "../../../contracts/cortex-api";
import { normalizeApiBaseUrl } from "./baseUrl";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

type FetchLike = typeof fetch;
type SessionExpiredListener = () => void;
type ValidationIssue = { loc?: unknown; msg?: unknown };
type ErrorBody = {
  detail?: string | { message?: string } | ValidationIssue[];
};

const SESSION_TOKEN_KEY = "cortex.session.token";
const VALIDATION_ISSUE_LIMIT = 8;
const VALIDATION_TEXT_LIMIT = 240;
const REQUEST_LOCATION_MARKERS = new Set(["body", "query", "path", "header", "cookie"]);

// C0 and C1 control characters. A validation message is rendered as text, so
// these are replaced rather than stripped: a control character between two
// words should leave a word boundary behind, and the whitespace collapse
// below then folds it away.
//
// no-control-regex exists to catch control characters that reached a pattern
// by accident. Matching them is the entire purpose of this one, and the rule
// cannot tell the difference.
// eslint-disable-next-line no-control-regex
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/g;

function cleanValidationText(value: string): string {
  return value
    .replace(CONTROL_CHARACTERS, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, VALIDATION_TEXT_LIMIT);
}

function formatValidationLocation(location: unknown): string | null {
  if (!Array.isArray(location) || location.length === 0) return null;

  const parts: string[] = [];
  for (const part of location) {
    if (typeof part === "string") {
      const cleaned = cleanValidationText(part);
      if (!cleaned) return null;
      parts.push(cleaned);
    } else if (typeof part === "number" && Number.isSafeInteger(part) && part >= 0) {
      parts.push(String(part));
    } else {
      return null;
    }
  }

  if (REQUEST_LOCATION_MARKERS.has(parts[0]?.toLowerCase() ?? "")) parts.shift();
  if (parts.length === 0) return null;

  return parts.reduce((path, part, index) => {
    if (/^\d+$/.test(part)) return index === 0 ? `[${part}]` : `${path}[${part}]`;
    return index === 0 ? part : `${path}.${part}`;
  }, "");
}

function formatValidationIssues(detail: ValidationIssue[]): string | null {
  const messages = detail.slice(0, VALIDATION_ISSUE_LIMIT).flatMap((issue) => {
    if (!issue || typeof issue !== "object" || Array.isArray(issue)) return [];
    const location = formatValidationLocation(issue.loc);
    const message = typeof issue.msg === "string" ? cleanValidationText(issue.msg) : "";
    if (!location || !message) return [];
    return [`${location}: ${message}`];
  });
  return messages.length > 0 ? messages.join("; ") : null;
}

function readPersistedSessionToken(): string | null {
  try {
    return window.sessionStorage.getItem(SESSION_TOKEN_KEY);
  } catch {
    // sessionStorage is an optional resilience layer. Browsers can deny both
    // access to the storage object and individual storage operations.
    return null;
  }
}

function persistSessionToken(token: string): void {
  try {
    window.sessionStorage.setItem(SESSION_TOKEN_KEY, token);
  } catch {
    // Keep the exchanged token in memory when persistence is unavailable.
  }
}

function removePersistedSessionToken(): void {
  try {
    window.sessionStorage.removeItem(SESSION_TOKEN_KEY);
  } catch {
    // Clearing the in-memory session and notifying subscribers still matters
    // when the browser denies storage access.
  }
}

export class CortexApi {
  private readonly baseUrl: string;
  private readonly fetcher: FetchLike;
  private sessionToken: string | null;
  private readonly sessionExpiredListeners = new Set<SessionExpiredListener>();

  constructor(
    baseUrl = import.meta.env.VITE_API_BASE_URL,
    fetcher: FetchLike = window.fetch.bind(window),
  ) {
    this.baseUrl = normalizeApiBaseUrl(baseUrl, import.meta.env.PROD);
    this.fetcher = fetcher;
    this.sessionToken = readPersistedSessionToken();
  }

  get hasSession(): boolean {
    return this.sessionToken !== null;
  }

  subscribeSessionExpired(listener: SessionExpiredListener): () => void {
    this.sessionExpiredListeners.add(listener);
    return () => this.sessionExpiredListeners.delete(listener);
  }

  clearSession(): void {
    const hadSession = this.sessionToken !== null;
    this.sessionToken = null;
    removePersistedSessionToken();
    if (hadSession) {
      for (const listener of this.sessionExpiredListeners) listener();
    }
  }

  async exchangeBootstrapToken(token: string): Promise<SessionExchangeResponse> {
    const response = await this.request<SessionExchangeResponse>(
      "/session/exchange",
      {
        method: "POST",
        body: JSON.stringify({ bootstrap_token: token }),
        authenticated: false,
      },
    );
    this.sessionToken = response.session_token;
    persistSessionToken(response.session_token);
    return response;
  }

  async rebootstrap(handoffSecret: string): Promise<SessionExchangeResponse> {
    const handoff = await this.request<HandoffResponse>("/session/handoff", {
      method: "POST",
      headers: { "X-Cortex-Handoff": handoffSecret },
      authenticated: false,
    });
    return this.exchangeBootstrapToken(handoff.bootstrap_token);
  }

  health(): Promise<HealthResponse> {
    return this.request<HealthResponse>("/health", { authenticated: false });
  }

  system(): Promise<SystemResponse> {
    return this.request<SystemResponse>("/system");
  }

  chats(): Promise<ChatSummary[]> {
    return this.request<ChatSummary[]>("/chats");
  }

  chat(threadId: string): Promise<ChatResponse> {
    return this.request<ChatResponse>(`/chats/${encodeURIComponent(threadId)}`);
  }

  createChat(title = "New Chat"): Promise<ChatResponse> {
    const payload: CreateChatRequest = { title };
    return this.request<ChatResponse>("/chats", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  renameChat(threadId: string, title: string): Promise<ChatResponse> {
    const payload: RenameChatRequest = { title };
    return this.request<ChatResponse>(
      `/chats/${encodeURIComponent(threadId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
    );
  }

  chatGroups(): Promise<ChatGroup[]> {
    return this.request<ChatGroup[]>("/chat-groups");
  }

  createChatGroup(name: string): Promise<ChatGroup> {
    const payload: CreateChatGroupRequest = { name };
    return this.request<ChatGroup>("/chat-groups", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  /** Rename and collapse share one endpoint; pass only what changed. */
  updateChatGroup(groupId: string, changes: UpdateChatGroupRequest): Promise<ChatGroup> {
    return this.request<ChatGroup>(
      `/chat-groups/${encodeURIComponent(groupId)}`,
      { method: "PATCH", body: JSON.stringify(changes) },
    );
  }

  /** Deletes the group only -- its chats return to the ungrouped list. */
  async deleteChatGroup(groupId: string): Promise<void> {
    await this.request<void>(`/chat-groups/${encodeURIComponent(groupId)}`, {
      method: "DELETE",
    });
  }

  /** Pass null to move the chat out of every group. */
  moveChatToGroup(threadId: string, groupId: string | null): Promise<ChatSummary> {
    const payload: MoveChatToGroupRequest = { group_id: groupId };
    return this.request<ChatSummary>(
      `/chats/${encodeURIComponent(threadId)}/group`,
      { method: "PATCH", body: JSON.stringify(payload) },
    );
  }

  forkChat(threadId: string, messageId: string): Promise<ChatResponse> {
    const payload: ForkRequest = { message_id: messageId };
    return this.request<ChatResponse>(
      `/chats/${encodeURIComponent(threadId)}/forks`,
      { method: "POST", body: JSON.stringify(payload) },
    );
  }

  generate(payload: GenerationRequest): Promise<JobAccepted> {
    return this.request<JobAccepted>("/generations", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  regenerate(threadId: string, payload: RegenerationRequest): Promise<JobAccepted> {
    return this.request<JobAccepted>(
      `/chats/${encodeURIComponent(threadId)}/regenerations`,
      { method: "POST", body: JSON.stringify(payload) },
    );
  }

  generationStatus(jobId: string): Promise<JobStatusResponse> {
    return this.request<JobStatusResponse>(`/generations/${encodeURIComponent(jobId)}`);
  }

  cancelGeneration(jobId: string): Promise<JobStatusResponse> {
    return this.request<JobStatusResponse>(
      `/generations/${encodeURIComponent(jobId)}/cancel`,
      { method: "POST" },
    );
  }

  streamGeneration(
    jobId: string,
    onEvent: (event: GenerationEvent) => void,
    options: { signal?: AbortSignal; afterEventId?: number } = {},
  ): Promise<void> {
    return this.streamEvents(`/generations/${encodeURIComponent(jobId)}/events`, onEvent, options).then(() => undefined);
  }

  private async streamEvents<T>(
    path: string,
    onEvent: (event: T) => void,
    options: { signal?: AbortSignal; afterEventId?: number } = {},
  ): Promise<T | null> {
    const headers = this.authHeaders();
    const sessionAtRequest = this.sessionToken;
    if (options.afterEventId !== undefined) {
      headers.set("Last-Event-ID", String(options.afterEventId));
    }
    const response = await this.fetcher(`${this.baseUrl}${path}`, {
      headers,
      signal: options.signal,
    });
    if (response.status === 401 && this.sessionToken === sessionAtRequest) {
      this.clearSession();
    }
    if (!response.ok || !response.body) {
      throw new ApiError(response.status, await this.errorDetail(response));
    }

    let terminalEvent: T | null = null;
    const emit = (frame: string) => {
      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("\n");
      if (!data) return;
      // A malformed frame must not take down an otherwise-live connection: if
      // Last-Event-ID resume then replays the same bad frame on reconnect,
      // throwing here turns one bad event into an infinite reconnect loop
      // with growing backoff instead of losing a single event.
      let event: T;
      try {
        event = JSON.parse(data) as T;
      } catch {
        console.warn("Cortex: skipping a malformed SSE frame", data.slice(0, 200));
        return;
      }
      onEvent(event);
      const status = (event as { status?: unknown }).status;
      if (status === "succeeded" || status === "failed" || status === "cancelled") {
        terminalEvent = event;
      }
    };
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value ?? new Uint8Array(), { stream: !chunk.done });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) emit(frame);
      if (chunk.done) break;
    }
    // The backend may close the stream without terminating the last frame with a
    // blank line, so anything left in the buffer is still a deliverable event.
    if (buffer.trim()) emit(buffer);
    return terminalEvent;
  }

  async deleteChat(threadId: string): Promise<void> {
    await this.request<void>(`/chats/${encodeURIComponent(threadId)}`, {
      method: "DELETE",
    });
  }

  settings(): Promise<SettingsResponse> {
    return this.request<SettingsResponse>("/settings");
  }

  updateSettings(settings: SettingsUpdateRequest): Promise<SettingsResponse> {
    return this.request<SettingsResponse>("/settings", {
      method: "PUT",
      body: JSON.stringify(settings),
    });
  }

  models(): Promise<ModelResponse> {
    return this.request<ModelResponse>("/models");
  }

  jobStatus(jobId: string): Promise<JobStatusResponse> {
    return this.request<JobStatusResponse>(`/jobs/${encodeURIComponent(jobId)}`);
  }

  diagnostics(): Promise<DiagnosticsResponse> {
    return this.request<DiagnosticsResponse>("/diagnostics");
  }

  checkModels(): Promise<JobAccepted> {
    return this.request<JobAccepted>("/jobs/models", { method: "POST" });
  }

  pullModel(model: string): Promise<JobAccepted> {
    const payload: ModelPullRequest = { model };
    return this.request<JobAccepted>("/models/pulls", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  listHuggingFaceGGUFFiles(repoId: string): Promise<HuggingFaceFileListResponse> {
    const params = new URLSearchParams({ repo_id: repoId });
    return this.request<HuggingFaceFileListResponse>(`/models/gguf/huggingface-files?${params.toString()}`);
  }

  downloadGGUFModel(payload: ModelDownloadRequest): Promise<JobAccepted> {
    return this.request<JobAccepted>("/models/gguf/downloads", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  cancelJob(jobId: string): Promise<JobStatusResponse> {
    return this.request<JobStatusResponse>(
      `/jobs/${encodeURIComponent(jobId)}/cancel`,
      { method: "POST" },
    );
  }

  executionTasks(options: { includeTerminal?: boolean; limit?: number } = {}): Promise<ExecutionTaskListResponse> {
    const params = new URLSearchParams();
    if (options.includeTerminal !== undefined) params.set("include_terminal", String(options.includeTerminal));
    if (options.limit !== undefined) params.set("limit", String(options.limit));
    const query = params.toString();
    return this.request<ExecutionTaskListResponse>(`/execution/tasks${query ? `?${query}` : ""}`);
  }

  executionStatus(jobId: string): Promise<ExecutionStatusResponse> {
    return this.request<ExecutionStatusResponse>(`/execution/${encodeURIComponent(jobId)}`);
  }

  startCodeExecution(payload: CodeExecutionRequest): Promise<CodeExecutionAccepted> {
    return this.request<CodeExecutionAccepted>("/execution/code", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  executionSource(jobId: string): Promise<CodeExecutionSourceResponse> {
    return this.request<CodeExecutionSourceResponse>(
      `/execution/${encodeURIComponent(jobId)}/source`,
    );
  }

  startScratchCompute(payload: ScratchComputeRequest): Promise<ScratchComputeAccepted> {
    return this.request<ScratchComputeAccepted>("/execution/scratch", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  startRecipeImageTransform(
    payload: RecipeImageTransformRequest,
  ): Promise<RecipeImageTransformAccepted> {
    return this.request<RecipeImageTransformAccepted>("/execution/recipe/image", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  stageAttachment(payload: AttachmentStageRequest): Promise<AttachmentStageAccepted> {
    return this.request<AttachmentStageAccepted>("/execution/attachments", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  stageChatAttachment(payload: ChatAttachmentStageRequest): Promise<ChatAttachment> {
    return this.request<ChatAttachment>("/attachments", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async downloadExecutionArtifact(artifactId: string): Promise<Response> {
    // Same guard request() uses: a 401 answering a request sent under an
    // older token says nothing about the token in hand now. Without it a
    // slow download could arrive after a re-exchange and sign the user out
    // of a session that was working.
    const sessionAtRequest = this.sessionToken;
    const response = await this.fetcher(
      `${this.baseUrl}/execution/artifacts/${encodeURIComponent(artifactId)}`,
      { headers: this.authHeaders() },
    );
    if (response.status === 401 && this.sessionToken === sessionAtRequest) this.clearSession();
    if (!response.ok) throw new ApiError(response.status, await this.errorDetail(response));
    return response;
  }

  cancelExecution(jobId: string): Promise<ExecutionStatusResponse> {
    return this.request<ExecutionStatusResponse>(
      `/execution/${encodeURIComponent(jobId)}/cancel`,
      { method: "POST" },
    );
  }

  decideExecutionApproval(
    jobId: string,
    decision: ExecutionApprovalDecisionRequest["decision"],
  ): Promise<ExecutionStatusResponse> {
    return this.request<ExecutionStatusResponse>(
      `/execution/${encodeURIComponent(jobId)}/approval`,
      {
        method: "POST",
        body: JSON.stringify({ decision }),
      },
    );
  }

  streamExecution(
    jobId: string,
    onEvent: (event: ExecutionSSEEvent) => void,
    options: { signal?: AbortSignal; afterEventId?: number } = {},
  ): Promise<void> {
    return this.streamEvents(`/execution/${encodeURIComponent(jobId)}/events`, onEvent, options).then(() => undefined);
  }

  streamJob(
    jobId: string,
    onEvent: (event: SSEEvent) => void,
    options: { signal?: AbortSignal; afterEventId?: number } = {},
  ): Promise<SSEEvent | null | void> {
    return this.streamEvents(`/jobs/${encodeURIComponent(jobId)}/events`, onEvent, options);
  }

  memories(): Promise<MemoryResponse> {
    return this.request<MemoryResponse>("/memories");
  }

  addMemory(memo: string): Promise<MemoryResponse> {
    const payload: AddMemoryRequest = { memo };
    return this.request<MemoryResponse>("/memories", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async clearMemories(): Promise<MemoryResponse> {
    return this.request<MemoryResponse>("/memories/clear", {
      method: "POST",
      body: JSON.stringify({ confirm: true, confirmation_intent: "clear_permanent_memory" }),
    });
  }

  replaceMemories(memos: string[]): Promise<MemoryResponse> {
    return this.request<MemoryResponse>("/memories", {
      method: "PUT",
      body: JSON.stringify({ memos }),
    });
  }

  shutdown(): Promise<ShutdownResponse> {
    return this.request<ShutdownResponse>("/system/shutdown", { method: "POST" });
  }

  private async request<T>(
    path: string,
    options: RequestInit & { authenticated?: boolean } = {},
  ): Promise<T> {
    const { authenticated = true, ...requestInit } = options;
    const headers = authenticated
      ? this.authHeaders(requestInit.headers)
      : new Headers(requestInit.headers);
    if (requestInit.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }

    const sessionAtRequest = authenticated ? this.sessionToken : null;
    const response = await this.fetcher(`${this.baseUrl}${path}`, {
      ...requestInit,
      headers,
    });
    if (response.status === 401 && authenticated && this.sessionToken === sessionAtRequest) {
      this.clearSession();
    }
    if (!response.ok) {
      const detail = await this.errorDetail(response);
      throw new ApiError(
        response.status,
        detail,
      );
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  private authHeaders(init?: HeadersInit): Headers {
    const headers = new Headers(init);
    if (this.sessionToken) {
      headers.set("Authorization", `Bearer ${this.sessionToken}`);
    }
    return headers;
  }

  private async errorDetail(response: Response): Promise<string> {
    const body = (await response.json().catch(() => null)) as ErrorBody | null;
    if (response.status === 422 && Array.isArray(body?.detail)) {
      const validationDetail = formatValidationIssues(body.detail);
      if (validationDetail) return validationDetail;
    }
    if (typeof body?.detail === "string") return body.detail;
    if (body?.detail && typeof body.detail === "object" && !Array.isArray(body.detail) && typeof body.detail.message === "string") {
      return body.detail.message;
    }
    return "The local workspace did not respond.";
  }
}
