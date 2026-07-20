import type {
  AddMemoryRequest,
  ChatResponse,
  ChatSummary,
  CreateChatRequest,
  HealthResponse,
  MemoryResponse,
  RenameChatRequest,
  SessionExchangeResponse,
  SettingsResponse,
  SettingsUpdateRequest,
  SystemResponse,
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
      body: JSON.stringify({ confirm: true }),
    });
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
    if (authenticated && this.sessionToken) {
      headers.set("Authorization", `Bearer ${this.sessionToken}`);
    }

    const response = await this.fetcher(`${this.baseUrl}${path}`, {
      ...requestInit,
      headers,
    });
    if (response.status === 401 && authenticated) {
      this.clearSession();
    }
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as
        | { detail?: string }
        | null;
      throw new ApiError(
        response.status,
        body?.detail ?? "Cortex API request failed.",
      );
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }
}
