import type {
  AddMemoryRequest,
  ChatResponse,
  ChatSummary,
  CreateChatRequest,
  DiagnosticsResponse,
  ForkRequest,
  GenerationEvent,
  GenerationRequest,
  ShutdownResponse,
  JobAccepted,
  JobStatusResponse,
  HealthResponse,
  MemoryResponse,
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

export class CortexApi {
  private readonly baseUrl: string;
  private readonly fetcher: FetchLike;
  private sessionToken: string | null;

  constructor(
    baseUrl = import.meta.env.VITE_API_BASE_URL ?? "/api/v1",
    fetcher: FetchLike = window.fetch.bind(window),
  ) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.fetcher = fetcher;
    this.sessionToken = window.sessionStorage.getItem("cortex.session.token");
  }

  get hasSession(): boolean {
    return this.sessionToken !== null;
  }

  clearSession(): void {
    this.sessionToken = null;
    window.sessionStorage.removeItem("cortex.session.token");
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
    window.sessionStorage.setItem("cortex.session.token", response.session_token);
    return response;
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

  async streamGeneration(
    jobId: string,
    onEvent: (event: GenerationEvent) => void,
    options: { signal?: AbortSignal; afterEventId?: number } = {},
  ): Promise<void> {
    const headers = this.authHeaders();
    if (options.afterEventId !== undefined) {
      headers.set("Last-Event-ID", String(options.afterEventId));
    }
    const response = await this.fetcher(
      `${this.baseUrl}/generations/${encodeURIComponent(jobId)}/events`,
      { headers, signal: options.signal },
    );
    if (response.status === 401) {
      this.clearSession();
    }
    if (!response.ok || !response.body) {
      throw new ApiError(response.status, await this.errorDetail(response));
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value ?? new Uint8Array(), { stream: !chunk.done });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const data = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim())
          .join("\n");
        if (!data) continue;
        onEvent(JSON.parse(data) as GenerationEvent);
      }
      if (chunk.done) break;
    }
    if (buffer.trim()) {
      const data = buffer
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("\n");
      if (data) onEvent(JSON.parse(data) as GenerationEvent);
    }
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

  cancelJob(jobId: string): Promise<JobStatusResponse> {
    return this.request<JobStatusResponse>(
      `/jobs/${encodeURIComponent(jobId)}/cancel`,
      { method: "POST" },
    );
  }

  async streamJob(
    jobId: string,
    onEvent: (event: SSEEvent) => void,
    options: { signal?: AbortSignal; afterEventId?: number } = {},
  ): Promise<void> {
    const headers = this.authHeaders();
    if (options.afterEventId !== undefined) {
      headers.set("Last-Event-ID", String(options.afterEventId));
    }
    const response = await this.fetcher(
      `${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/events`,
      { headers, signal: options.signal },
    );
    if (!response.ok || !response.body) {
      throw new ApiError(response.status, await this.errorDetail(response));
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value ?? new Uint8Array(), { stream: !chunk.done });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const data = frame.split("\n").filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim()).join("\n");
        if (data) onEvent(JSON.parse(data) as SSEEvent);
      }
      if (chunk.done) break;
    }
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

    const response = await this.fetcher(`${this.baseUrl}${path}`, {
      ...requestInit,
      headers,
    });
    if (response.status === 401 && authenticated) {
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
    const body = (await response.json().catch(() => null)) as
      | { detail?: string | { message?: string } }
      | null;
    if (typeof body?.detail === "string") return body.detail;
    if (body?.detail && typeof body.detail.message === "string") {
      return body.detail.message;
    }
    return "Cortex API request failed.";
  }
}
