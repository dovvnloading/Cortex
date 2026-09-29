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

/**
 * What kind of failure an {@link ApiError} is, so callers stop inferring it
 * from a status code that some of them do not have:
 * - `http`: the backend answered with an error status.
 * - `validation`: it answered 422 -- the request itself is wrong, so sending
 *   the same request again cannot succeed.
 * - `auth`: it answered 401 -- the session is missing or expired.
 * - `network`: no answer at all (status 0). The backend may still have acted
 *   on the request, so an idempotent replay is the only safe retry.
 * - `aborted`: the caller cancelled the request (status 0). Not a failure.
 */
export type ApiErrorKind = "http" | "validation" | "auth" | "network" | "aborted";

const NETWORK_ERROR_DETAIL = "Cortex could not reach the local backend. Check that it is still running, then try again.";
const ABORTED_ERROR_DETAIL = "The request was cancelled.";

function kindForStatus(status: number): ApiErrorKind {
  if (status === 401) return "auth";
  if (status === 422) return "validation";
  return "http";
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly kind: ApiErrorKind;

  constructor(status: number, detail: string, kind: ApiErrorKind = kindForStatus(status)) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.kind = kind;
  }
}

/**
 * The text to show for a failed call: the backend's own sentence for an
 * {@link ApiError}, the caller's `fallback` for anything else. Replaces the
 * `error instanceof ApiError ? error.detail : "..."` ternary each call site
 * used to carry.
 */
export function describeApiError(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.detail : fallback;
}

/** True when the caller cancelled the request, which is never worth reporting. */
export function isAbortedError(error: unknown): boolean {
  return error instanceof ApiError && error.kind === "aborted";
}

/**
 * True when the backend answered with a client-side rejection, so the request
 * definitely did not take effect and its idempotency key is spent. A network
 * failure or a 5xx is not: the backend may have acted before the answer was
 * lost, and only replaying the same request id is safe.
 */
export function isDefinitiveRejection(error: unknown): boolean {
  return error instanceof ApiError
    && error.kind !== "network"
    && error.kind !== "aborted"
    && error.status >= 400
    && error.status < 500;
}

/**
 * Give a transport failure a kind. fetch rejects with a bare `TypeError` when
 * the backend cannot be reached and with an `AbortError` when the caller
 * cancels; neither says which it was in a way call sites can rely on, and both
 * used to reach the UI as an unclassified exception. Anything else is not a
 * transport failure and passes through unchanged.
 */
function transportError(error: unknown, signal?: AbortSignal | null): unknown {
  if (error instanceof ApiError) return error;
  const name = typeof error === "object" && error !== null ? (error as { name?: unknown }).name : undefined;
  if (signal?.aborted || name === "AbortError") return new ApiError(0, ABORTED_ERROR_DETAIL, "aborted");
  if (error instanceof TypeError) return new ApiError(0, NETWORK_ERROR_DETAIL, "network");
  return error;
}

type FetchLike = typeof fetch;
type SessionExpiredListener = () => void;
type ValidationIssue = { loc?: unknown; msg?: unknown };
type ErrorBody = {
  detail?: string | { message?: string } | ValidationIssue[];
};

const SESSION_TOKEN_KEY = "cortex.session.token";
// The launcher hands the handoff secret over once, in the URL fragment. It
// stays valid for the life of the backend process and is the only way to
// re-exchange an expired session, so it has to survive a reload the same way
// the session token does -- otherwise the app's own "Reload workspace"
// button leaves the next session expiry unrecoverable.
const HANDOFF_SECRET_KEY = "cortex.session.handoff";
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

export function readPersistedHandoffSecret(): string {
  try {
    return window.sessionStorage.getItem(HANDOFF_SECRET_KEY) ?? "";
  } catch {
    return "";
  }
}

export function persistHandoffSecret(secret: string): void {
  if (!secret) return;
  try {
    window.sessionStorage.setItem(HANDOFF_SECRET_KEY, secret);
  } catch {
    // Best effort, exactly like the session token above.
  }
}

export class CortexApi {
  private readonly baseUrl: string;
  private readonly fetcher: FetchLike;
  private sessionToken: string | null;
  private handoffSecret: string | null = null;
  private renewal: Promise<boolean> | null = null;
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

  /**
   * Give the client the launcher's handoff secret so an expired session can be
   * renewed in place. Without one a 401 cannot be recovered here and the
   * session is cleared for the subscribers to deal with.
   */
  setHandoffSecret(secret: string): void {
    this.handoffSecret = secret || null;
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

  generationStatus(jobId: string, options: { signal?: AbortSignal } = {}): Promise<JobStatusResponse> {
    return this.request<JobStatusResponse>(
      `/generations/${encodeURIComponent(jobId)}`,
      { signal: options.signal },
    );
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
    const headers = new Headers();
    if (options.afterEventId !== undefined) {
      headers.set("Last-Event-ID", String(options.afterEventId));
    }
    const response = await this.fetchWithSession(`${this.baseUrl}${path}`, {
      headers,
      signal: options.signal,
    });
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
      // A connection that drops mid-stream rejects the read with a bare
      // TypeError; classify it like a failed connect. Only the read is
      // wrapped: an error from onEvent is the caller's and must pass through.
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch (error) {
        throw transportError(error, options.signal);
      }
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
    const response = await this.fetchWithSession(
      `${this.baseUrl}/execution/artifacts/${encodeURIComponent(artifactId)}`,
      {},
    );
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
    const headers = new Headers(requestInit.headers);
    if (requestInit.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }

    const url = `${this.baseUrl}${path}`;
    const init = { ...requestInit, headers };
    const response = authenticated
      ? await this.fetchWithSession(url, init)
      : await this.send(url, init);
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

  /**
   * Fetch with the session bearer, recovering from an expired session once.
   *
   * A 401 comes from the session check, before any handler runs, so replaying
   * the request cannot apply it twice. On a 401:
   * - if the client already holds a newer session than the one the request was
   *   sent under (another request renewed it first), replay under that one;
   * - if the request's own session is still current, renew it through the
   *   launcher handoff -- one renewal shared by every request that fails
   *   meanwhile -- and replay;
   * - if renewal is unavailable or fails, or the replay is rejected as well,
   *   clear the session, which notifies the subscribers, and return the 401.
   *
   * A 401 answering a request sent under an older token never clears a newer
   * session, which is what keeps a slow request from signing the user out of a
   * session that was working.
   */
  private async fetchWithSession(url: string, init: RequestInit): Promise<Response> {
    let replayed = false;
    while (true) {
      const tokenSent = this.sessionToken;
      const response = await this.send(url, { ...init, headers: this.authHeaders(init.headers) });
      if (response.status !== 401) return response;
      if (!replayed && tokenSent !== null && (await this.sessionAfterRejection(tokenSent))) {
        replayed = true;
        continue;
      }
      if (this.sessionToken === tokenSent) this.clearSession();
      return response;
    }
  }

  /** The one place fetch is called, so every transport failure is classified. */
  private async send(url: string, init: RequestInit): Promise<Response> {
    try {
      return await this.fetcher(url, init);
    } catch (error) {
      throw transportError(error, init.signal);
    }
  }

  /** True when the client now holds a usable session other than `rejectedToken`. */
  private async sessionAfterRejection(rejectedToken: string): Promise<boolean> {
    if (this.sessionToken !== rejectedToken) return this.sessionToken !== null;
    return this.renewSession();
  }

  /** Re-exchange through the launcher handoff. Single flight; never rejects. */
  private renewSession(): Promise<boolean> {
    const secret = this.handoffSecret;
    if (!secret) return Promise.resolve(false);
    if (this.renewal === null) {
      this.renewal = (async () => {
        try {
          await this.rebootstrap(secret);
          return true;
        } catch {
          // The caller clears the session; the app's own reconnect reports why.
          return false;
        } finally {
          this.renewal = null;
        }
      })();
    }
    return this.renewal;
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
