import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, CortexApi, describeApiError, isAbortedError, isDefinitiveRejection } from "./client";

describe("CortexApi", () => {
  afterEach(() => window.sessionStorage.clear());

  it("starts without throwing when sessionStorage access is denied", () => {
    const storageGetter = vi.spyOn(window, "sessionStorage", "get").mockImplementation(() => {
      throw new DOMException("Storage access denied", "SecurityError");
    });

    try {
      const api = new CortexApi("/api/v1", vi.fn<typeof fetch>());

      expect(api.hasSession).toBe(false);
    } finally {
      storageGetter.mockRestore();
    }
  });

  it("keeps the exchanged session in memory when sessionStorage persistence fails", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(
      JSON.stringify({ session_token: "session-1", expires_at: "2026-07-20T00:00:00Z" }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    const originalStorage = window.sessionStorage;
    const setItem = vi.fn<Storage["setItem"]>(() => {
      throw new DOMException("Storage quota exceeded", "QuotaExceededError");
    });
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      value: {
        clear: originalStorage.clear.bind(originalStorage),
        getItem: originalStorage.getItem.bind(originalStorage),
        key: originalStorage.key.bind(originalStorage),
        length: originalStorage.length,
        removeItem: originalStorage.removeItem.bind(originalStorage),
        setItem,
      } as Storage,
    });
    const api = new CortexApi("/api/v1", fetcher);

    try {
      await expect(api.exchangeBootstrapToken("bootstrap")).resolves.toMatchObject({
        session_token: "session-1",
      });
      expect(setItem).toHaveBeenCalledWith("cortex.session.token", "session-1");
      expect(api.hasSession).toBe(true);
    } finally {
      Object.defineProperty(window, "sessionStorage", {
        configurable: true,
        value: originalStorage,
      });
    }
  });

  it("notifies session listeners and stays safe when clearing storage fails", () => {
    const originalStorage = window.sessionStorage;
    originalStorage.setItem("cortex.session.token", "session-1");
    const removeItem = vi.fn<Storage["removeItem"]>(() => {
      throw new DOMException("Storage access denied", "SecurityError");
    });
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      value: {
        clear: originalStorage.clear.bind(originalStorage),
        getItem: originalStorage.getItem.bind(originalStorage),
        key: originalStorage.key.bind(originalStorage),
        length: originalStorage.length,
        removeItem,
        setItem: originalStorage.setItem.bind(originalStorage),
      } as Storage,
    });
    const api = new CortexApi("/api/v1", vi.fn<typeof fetch>());
    const onSessionExpired = vi.fn();
    api.subscribeSessionExpired(onSessionExpired);

    try {
      expect(() => api.clearSession()).not.toThrow();
      expect(api.hasSession).toBe(false);
      expect(removeItem).toHaveBeenCalledWith("cortex.session.token");
      expect(onSessionExpired).toHaveBeenCalledOnce();
    } finally {
      Object.defineProperty(window, "sessionStorage", {
        configurable: true,
        value: originalStorage,
      });
    }
  });

  it("exchanges a bootstrap token and sends the session bearer on protected calls", async () => {
    const fetcher = vi.fn<typeof fetch>();
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ session_token: "session-1", expires_at: "2026-07-20T00:00:00Z" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ status: "ok", preview: true, started_at: "2026-07-20T00:00:00Z" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const api = new CortexApi("/api/v1", fetcher);

    await api.exchangeBootstrapToken("bootstrap");
    await api.system();

    expect(fetcher).toHaveBeenNthCalledWith(1, "/api/v1/session/exchange", expect.objectContaining({ method: "POST" }));
    expect(fetcher).toHaveBeenNthCalledWith(2, "/api/v1/system", expect.objectContaining({ headers: expect.any(Headers) }));
    const secondRequest = fetcher.mock.calls[1]?.[1] as RequestInit;
    expect(new Headers(secondRequest.headers).get("Authorization")).toBe("Bearer session-1");
    expect(api.hasSession).toBe(true);
  });

  it("rebootstraps an expired desktop session through the launcher handoff", async () => {
    const fetcher = vi.fn<typeof fetch>();
    fetcher.mockResolvedValueOnce(new Response(
      JSON.stringify({ bootstrap_token: "fresh-bootstrap", expires_at: "2026-07-20T00:00:00Z" }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    fetcher.mockResolvedValueOnce(new Response(
      JSON.stringify({ session_token: "session-2", expires_at: "2026-07-20T01:00:00Z" }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.rebootstrap("desktop-handoff")).resolves.toMatchObject({
      session_token: "session-2",
    });

    const handoffRequest = fetcher.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(handoffRequest.headers).get("X-Cortex-Handoff")).toBe("desktop-handoff");
    expect(new Headers(handoffRequest.headers).get("Authorization")).toBeNull();
    expect(handoffRequest.body).toBeUndefined();
    expect(api.hasSession).toBe(true);
  });

  it("turns safe API errors into ApiError without assuming a response body", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response("", { status: 503 }));
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.health()).rejects.toEqual(new ApiError(503, "The local workspace did not respond."));
  });

  it("turns FastAPI validation details into field-specific messages without exposing inputs", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(
      JSON.stringify({
        detail: [
          { loc: ["body", "name"], msg: "Field required", input: "private-name" },
          { loc: ["body", "items", 0, "label"], msg: "Field required", input: "private-label" },
        ],
      }),
      { status: 422, headers: { "Content-Type": "application/json" } },
    ));
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.health()).rejects.toEqual(new ApiError(
      422,
      "name: Field required; items[0].label: Field required",
    ));
  });

  it("falls back when validation details are malformed", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(
      JSON.stringify({
        detail: [
          { loc: "body.name", msg: "Do not expose this" },
          { loc: ["body", "name"], msg: { value: "Do not expose this" }, input: "private-input" },
        ],
      }),
      { status: 422, headers: { "Content-Type": "application/json" } },
    ));
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.health()).rejects.toEqual(new ApiError(422, "The local workspace did not respond."));
  });

  it("notifies subscribers when an authenticated request expires the session", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(
      JSON.stringify({ detail: "Local session expired." }),
      { status: 401, headers: { "Content-Type": "application/json" } },
    ));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);
    const onSessionExpired = vi.fn();
    api.subscribeSessionExpired(onSessionExpired);

    await expect(api.system()).rejects.toEqual(new ApiError(401, "Local session expired."));
    expect(onSessionExpired).toHaveBeenCalledOnce();
  });

  it("does not clear a replacement session when an older request returns 401", async () => {
    let releaseExpiredRequest!: (response: Response) => void;
    const expiredRequest = new Promise<Response>((resolve) => { releaseExpiredRequest = resolve; });
    const fetcher = vi.fn<typeof fetch>();
    fetcher.mockReturnValueOnce(expiredRequest);
    fetcher.mockResolvedValueOnce(new Response(
      JSON.stringify({ session_token: "session-2", expires_at: "2026-07-20T01:00:00Z" }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    // The request the old token was refused for is sent again under the new one.
    fetcher.mockResolvedValueOnce(new Response(
      JSON.stringify({ status: "ok", preview: true, started_at: "2026-07-20T00:00:00Z" }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);
    const onSessionExpired = vi.fn();
    api.subscribeSessionExpired(onSessionExpired);

    const pending = api.system();
    await api.exchangeBootstrapToken("fresh-bootstrap");
    releaseExpiredRequest(new Response(JSON.stringify({ detail: "Local session expired." }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    }));

    await expect(pending).resolves.toMatchObject({ status: "ok" });
    expect(onSessionExpired).not.toHaveBeenCalled();
    expect(api.hasSession).toBe(true);
    expect(window.sessionStorage.getItem("cortex.session.token")).toBe("session-2");
    const replay = fetcher.mock.calls[2]?.[1] as RequestInit;
    expect(new Headers(replay.headers).get("Authorization")).toBe("Bearer session-2");
  });

  it("does not clear a replacement session when an older SSE request returns 401", async () => {
    let releaseExpiredStream!: (response: Response) => void;
    const expiredStream = new Promise<Response>((resolve) => { releaseExpiredStream = resolve; });
    const fetcher = vi.fn<typeof fetch>();
    fetcher.mockReturnValueOnce(expiredStream);
    fetcher.mockResolvedValueOnce(new Response(
      JSON.stringify({ session_token: "session-2", expires_at: "2026-07-20T01:00:00Z" }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    fetcher.mockResolvedValueOnce(new Response(
      'id: 1\ndata: {"id":1,"job_id":"job-1","kind":"completed","status":"succeeded"}\n\n',
      { status: 200 },
    ));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);
    const onSessionExpired = vi.fn();
    api.subscribeSessionExpired(onSessionExpired);

    const pending = api.streamJob("job-1", vi.fn());
    await api.exchangeBootstrapToken("fresh-bootstrap");
    releaseExpiredStream(new Response(JSON.stringify({ detail: "Local session expired." }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    }));

    await expect(pending).resolves.toMatchObject({ status: "succeeded" });
    expect(onSessionExpired).not.toHaveBeenCalled();
    expect(api.hasSession).toBe(true);
    const replay = fetcher.mock.calls[2]?.[1] as RequestInit;
    expect(new Headers(replay.headers).get("Authorization")).toBe("Bearer session-2");
  });

  it("parses ordered authenticated generation events from an SSE response", async () => {
    const sse = [
      'id: 1\nevent: generation.queued\ndata: {"event_id":1,"event":"generation.queued","job_id":"job-1","thread_id":"thread-1","data":{}}\n\n',
      'id: 2\nevent: generation.content_delta\ndata: {"event_id":2,"event":"generation.content_delta","job_id":"job-1","thread_id":"thread-1","data":{"delta":"hello"}}\n\n',
    ].join("");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(sse, { status: 200 }));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);
    const events: string[] = [];

    await api.streamGeneration("job-1", (event) => events.push(event.event), { afterEventId: 0 });

    expect(events).toEqual(["generation.queued", "generation.content_delta"]);
    const request = fetcher.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).get("Authorization")).toBe("Bearer session-1");
    expect(new Headers(request.headers).get("Last-Event-ID")).toBe("0");
  });

  it("skips a malformed SSE frame instead of failing the whole stream", async () => {
    const sse = [
      'id: 1\nevent: generation.queued\ndata: {"event_id":1,"event":"generation.queued","job_id":"job-1","thread_id":"thread-1","data":{}}\n\n',
      // Truncated JSON -- a real-world symptom of a proxy or backend chunking
      // bug. Must not fail the connection or block later, valid frames.
      'id: 2\nevent: generation.content_delta\ndata: {"event_id":2,"event":"generation.content_delta","thread_id":"thread-1","data":{"delta":\n\n',
      'id: 3\nevent: generation.content_delta\ndata: {"event_id":3,"event":"generation.content_delta","job_id":"job-1","thread_id":"thread-1","data":{"delta":"hello"}}\n\n',
    ].join("");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(sse, { status: 200 }));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const events: string[] = [];

    await expect(
      api.streamGeneration("job-1", (event) => events.push(event.event), { afterEventId: 0 }),
    ).resolves.toBeUndefined();

    expect(events).toEqual(["generation.queued", "generation.content_delta"]);
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it("delivers a final SSE frame the stream closed without terminating", async () => {
    const sse = [
      'id: 1\ndata: {"id":1,"job_id":"job-1","kind":"state","status":"running"}\n\n',
      'id: 2\ndata: {"id":2,"job_id":"job-1","kind":"completed","status":"succeeded"}',
    ].join("");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(sse, { status: 200 }));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);
    const kinds: string[] = [];

    const terminal = await api.streamJob("job-1", (event) => kinds.push(event.kind));

    expect(kinds).toEqual(["state", "completed"]);
    expect(terminal).toMatchObject({ kind: "completed", status: "succeeded" });
  });

  it("returns no terminal event when a job stream closes while still active", async () => {
    const sse = 'id: 1\ndata: {"id":1,"job_id":"job-1","kind":"progress","status":"running"}\n\n';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(sse, { status: 200 }));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.streamJob("job-1", vi.fn())).resolves.toBeNull();
  });

  it("clears the session when a model job event stream returns 401", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(
      JSON.stringify({ detail: "Local session expired." }),
      { status: 401, headers: { "Content-Type": "application/json" } },
    ));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.streamJob("job-1", vi.fn())).rejects.toEqual(new ApiError(401, "Local session expired."));
    expect(api.hasSession).toBe(false);
    expect(window.sessionStorage.getItem("cortex.session.token")).toBeNull();
  });

  it("binds an approval decision to the encoded execution job route", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      job_id: "job/approval",
      request_id: "request-1",
      profile: "artifact.extended.v1",
      status: "queued",
      sequence: 3,
      approval_state: "approved",
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);

    await api.decideExecutionApproval("job/approval", "approved");

    expect(fetcher).toHaveBeenCalledWith(
      "/api/v1/execution/job%2Fapproval/approval",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ decision: "approved" }) }),
    );
    const request = fetcher.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).get("Authorization")).toBe("Bearer session-1");
  });

  it("unloads the local model with an authenticated POST and returns the new runtime status", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      state: "idle",
      binary_present: true,
      models_directory: "C:/synthetic/models",
      last_restart_reason: "the model was unloaded at your request",
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);

    const status = await api.unloadLlamaCpp();

    expect(fetcher).toHaveBeenCalledWith("/api/v1/llamacpp/unload", expect.objectContaining({ method: "POST" }));
    const request = fetcher.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).get("Authorization")).toBe("Bearer session-1");
    expect(status.state).toBe("idle");
    expect(status.last_restart_reason).toBe("the model was unloaded at your request");
  });

  it("reports a refused unload with the backend's own sentence", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(
      JSON.stringify({ detail: "A response is being generated. Stop it or wait for it to finish, then unload the model." }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    ));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.unloadLlamaCpp()).rejects.toMatchObject({
      status: 409,
      detail: expect.stringContaining("being generated"),
    });
  });

  it("starts a typed recipe request on the recipe route", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      job_id: "recipe-job",
      request_id: "recipe-request",
      profile: "recipe.image.v1",
      status: "queued",
      sequence: 1,
    }), { status: 202, headers: { "Content-Type": "application/json" } }));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);

    await api.startRecipeImageTransform({
      request_id: "recipe-request",
      source_artifact_id: "artifact-1",
      plan: {
        schema_version: "artifact.transform.v1",
        input_artifact_id: "artifact-1",
        steps: [{ op: "grayscale" }],
        output_format: "png",
      },
    });

    expect(fetcher).toHaveBeenCalledWith(
      "/api/v1/execution/recipe/image",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining('"source_artifact_id":"artifact-1"'),
      }),
    );
    const request = fetcher.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).get("Authorization")).toBe("Bearer session-1");
  });

  it("stages a bounded attachment through the attachment route", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      job_id: "attachment-job",
      request_id: "attachment-request",
      profile: "attachment.stage.v1",
      status: "succeeded",
      sequence: 1,
      artifact_id: "artifact-1",
      mime_type: "image/png",
      size: 4,
      sha256: "a".repeat(64),
      expires_at: "2026-07-20T00:00:00Z",
    }), { status: 201, headers: { "Content-Type": "application/json" } }));
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const api = new CortexApi("/api/v1", fetcher);

    await api.stageAttachment({
      request_id: "attachment-request",
      content_base64: "iVBORw==",
    });

    expect(fetcher).toHaveBeenCalledWith(
      "/api/v1/execution/attachments",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ request_id: "attachment-request", content_base64: "iVBORw==" }),
      }),
    );
  });
});

describe("session invalidation guards", () => {
  it("does not clear a session that was re-exchanged while a download was in flight", async () => {
    // A 401 answering a request sent under an older token says nothing about
    // the token in hand now. request() already guards this; the artifact
    // download did not, so a slow download could sign the user out of a
    // session that was working. It is sent again under the current token.
    let releaseDownload: (() => void) | null = null;
    let downloads = 0;
    let exchanges = 0;
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.includes("/session/exchange")) {
        exchanges += 1;
        return new Response(
          JSON.stringify({ session_token: `token-${exchanges}`, expires_at: "2099-01-01T00:00:00Z" }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      downloads += 1;
      if (downloads > 1) return new Response("artifact bytes", { status: 200 });
      await new Promise<void>((resolve) => {
        releaseDownload = resolve;
      });
      return new Response(JSON.stringify({ detail: "expired" }), { status: 401 });
    });
    const api = new CortexApi("/api/v1", fetcher);
    await api.exchangeBootstrapToken("bootstrap-1");
    expect(api.hasSession).toBe(true);

    const pending = api.downloadExecutionArtifact("artifact-1");
    await vi.waitFor(() => expect(releaseDownload).not.toBeNull());
    // The user re-exchanges while that download is still open.
    await api.exchangeBootstrapToken("bootstrap-2");
    releaseDownload!();
    const response = await pending;

    expect(await response.text()).toBe("artifact bytes");
    expect(api.hasSession).toBe(true);
    const replay = fetcher.mock.calls.filter(([url]) => String(url).includes("/artifacts/"))[1]?.[1] as RequestInit;
    expect(new Headers(replay.headers).get("Authorization")).toBe("Bearer token-2");
  });
});

describe("session renewal", () => {
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
  const bearer = (init: unknown) => new Headers((init as RequestInit).headers).get("Authorization");
  const sse = (body: string) => new Response(body, { status: 200 });

  /**
   * A backend whose session `expired-token` is no longer accepted. The handoff
   * and exchange routes behave as the real ones do, and every call is recorded.
   */
  function expiringBackend(options: { handoffStatus?: number; rejectRenewed?: boolean } = {}) {
    const rejected = new Set(["Bearer expired-token"]);
    let exchanges = 0;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/session/handoff")) {
        if (options.handoffStatus) return json({ detail: "Handoff refused." }, options.handoffStatus);
        return json({ bootstrap_token: `bootstrap-${exchanges + 1}`, expires_at: "2099-01-01T00:00:00Z" });
      }
      if (url.endsWith("/session/exchange")) {
        exchanges += 1;
        return json({ session_token: `renewed-${exchanges}`, expires_at: "2099-01-01T00:00:00Z", token_type: "bearer" });
      }
      const token = bearer(init);
      if ((token !== null && rejected.has(token)) || (options.rejectRenewed && token?.startsWith("Bearer renewed-"))) {
        return json({ detail: "Local session expired." }, 401);
      }
      if (url.includes("/artifacts/")) return new Response("artifact bytes", { status: 200 });
      if (url.endsWith("/events")) {
        return sse('id: 4\ndata: {"id":4,"job_id":"job-1","kind":"completed","status":"succeeded"}\n\n');
      }
      return json({ status: "ok", preview: true, started_at: "2026-07-20T00:00:00Z" });
    });
    const calls = (suffix: string) => fetcher.mock.calls.filter(([input]) => String(input).endsWith(suffix));
    return { fetcher, calls, rejected };
  }

  function apiWithExpiredSession(fetcher: typeof fetch, handoffSecret: string | null = "desktop-handoff") {
    window.sessionStorage.setItem("cortex.session.token", "expired-token");
    const api = new CortexApi("/api/v1", fetcher);
    if (handoffSecret !== null) api.setHandoffSecret(handoffSecret);
    const onSessionExpired = vi.fn();
    api.subscribeSessionExpired(onSessionExpired);
    return { api, onSessionExpired };
  }

  afterEach(() => window.sessionStorage.clear());

  it("renews an expired session once and replays the request under the new token", async () => {
    const { fetcher, calls } = expiringBackend();
    const { api, onSessionExpired } = apiWithExpiredSession(fetcher);

    await expect(api.system()).resolves.toMatchObject({ status: "ok" });

    expect(calls("/session/handoff")).toHaveLength(1);
    expect(new Headers((calls("/session/handoff")[0]?.[1] as RequestInit).headers).get("X-Cortex-Handoff")).toBe("desktop-handoff");
    expect(calls("/session/exchange")).toHaveLength(1);
    expect(calls("/system")).toHaveLength(2);
    expect(bearer(calls("/system")[1]?.[1])).toBe("Bearer renewed-1");
    expect(onSessionExpired).not.toHaveBeenCalled();
    expect(api.hasSession).toBe(true);
    expect(window.sessionStorage.getItem("cortex.session.token")).toBe("renewed-1");
  });

  it("replays a request with its original body", async () => {
    const { fetcher, calls } = expiringBackend();
    const { api } = apiWithExpiredSession(fetcher);

    await api.createChat("Kept body");

    const sent = calls("/chats").map(([, init]) => (init as RequestInit).body);
    expect(sent).toEqual([JSON.stringify({ title: "Kept body" }), JSON.stringify({ title: "Kept body" })]);
  });

  it("shares one renewal between requests that expire together", async () => {
    const { fetcher, calls } = expiringBackend();
    const { api, onSessionExpired } = apiWithExpiredSession(fetcher);

    const results = await Promise.all([api.system(), api.chats(), api.settings()]);

    expect(results).toHaveLength(3);
    expect(calls("/session/handoff")).toHaveLength(1);
    expect(calls("/session/exchange")).toHaveLength(1);
    for (const path of ["/system", "/chats", "/settings"]) {
      expect(calls(path)).toHaveLength(2);
      expect(bearer(calls(path)[1]?.[1])).toBe("Bearer renewed-1");
    }
    expect(onSessionExpired).not.toHaveBeenCalled();
  });

  it("can renew again the next time the session expires", async () => {
    const { fetcher, calls, rejected } = expiringBackend();
    const { api } = apiWithExpiredSession(fetcher);

    await api.system();
    // The renewed session lapses too, an hour later.
    rejected.add("Bearer renewed-1");
    await api.system();

    expect(calls("/session/handoff")).toHaveLength(2);
    expect(bearer(calls("/system")[3]?.[1])).toBe("Bearer renewed-2");
    expect(window.sessionStorage.getItem("cortex.session.token")).toBe("renewed-2");
  });

  it("does not replay, and clears the session, when renewal is refused", async () => {
    const { fetcher, calls } = expiringBackend({ handoffStatus: 403 });
    const { api, onSessionExpired } = apiWithExpiredSession(fetcher);

    await expect(api.system()).rejects.toEqual(new ApiError(401, "Local session expired."));

    expect(calls("/session/handoff")).toHaveLength(1);
    expect(calls("/session/exchange")).toHaveLength(0);
    expect(calls("/system")).toHaveLength(1);
    expect(onSessionExpired).toHaveBeenCalledOnce();
    expect(api.hasSession).toBe(false);
    expect(window.sessionStorage.getItem("cortex.session.token")).toBeNull();
  });

  it("clears the session when renewal cannot reach the backend", async () => {
    let handoffs = 0;
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      if (String(input).endsWith("/session/handoff")) {
        handoffs += 1;
        throw new TypeError("Failed to fetch");
      }
      return json({ detail: "Local session expired." }, 401);
    });
    const { api, onSessionExpired } = apiWithExpiredSession(fetcher);

    await expect(api.system()).rejects.toEqual(new ApiError(401, "Local session expired."));

    expect(handoffs).toBe(1);
    expect(onSessionExpired).toHaveBeenCalledOnce();
    expect(api.hasSession).toBe(false);
  });

  it("replays only once when the renewed session is rejected as well", async () => {
    const { fetcher, calls } = expiringBackend({ rejectRenewed: true });
    const { api, onSessionExpired } = apiWithExpiredSession(fetcher);

    await expect(api.system()).rejects.toEqual(new ApiError(401, "Local session expired."));

    expect(calls("/session/handoff")).toHaveLength(1);
    expect(calls("/system")).toHaveLength(2);
    expect(onSessionExpired).toHaveBeenCalledOnce();
    expect(api.hasSession).toBe(false);
  });

  it("does not try to renew without a handoff secret", async () => {
    const { fetcher, calls } = expiringBackend();
    const { api, onSessionExpired } = apiWithExpiredSession(fetcher, null);

    await expect(api.system()).rejects.toEqual(new ApiError(401, "Local session expired."));

    expect(calls("/session/handoff")).toHaveLength(0);
    expect(calls("/system")).toHaveLength(1);
    expect(onSessionExpired).toHaveBeenCalledOnce();
  });

  it("does not renew when the handoff request itself is rejected", async () => {
    const fetcher = vi.fn<typeof fetch>(async () => json({ detail: "Handoff refused." }, 401));
    const { api, onSessionExpired } = apiWithExpiredSession(fetcher);

    await expect(api.rebootstrap("desktop-handoff")).rejects.toEqual(new ApiError(401, "Handoff refused."));

    expect(fetcher).toHaveBeenCalledOnce();
    expect(onSessionExpired).not.toHaveBeenCalled();
  });

  it("does not renew a request that was sent without a session", async () => {
    const fetcher = vi.fn<typeof fetch>(async () => json({ detail: "Not signed in." }, 401));
    const api = new CortexApi("/api/v1", fetcher);
    api.setHandoffSecret("desktop-handoff");
    const onSessionExpired = vi.fn();
    api.subscribeSessionExpired(onSessionExpired);

    await expect(api.system()).rejects.toEqual(new ApiError(401, "Not signed in."));

    // Onboarding owns the first exchange; there is no session to renew.
    expect(fetcher).toHaveBeenCalledOnce();
    expect(onSessionExpired).not.toHaveBeenCalled();
  });

  it("renews and reconnects an event stream, keeping the resume cursor", async () => {
    const { fetcher, calls } = expiringBackend();
    const { api, onSessionExpired } = apiWithExpiredSession(fetcher);
    const kinds: string[] = [];

    const terminal = await api.streamJob("job-1", (event) => kinds.push(event.kind), { afterEventId: 3 });

    expect(terminal).toMatchObject({ status: "succeeded" });
    expect(kinds).toEqual(["completed"]);
    const attempts = calls("/jobs/job-1/events");
    expect(attempts).toHaveLength(2);
    for (const [, init] of attempts) {
      expect(new Headers((init as RequestInit).headers).get("Last-Event-ID")).toBe("3");
    }
    expect(bearer(attempts[1]?.[1])).toBe("Bearer renewed-1");
    expect(onSessionExpired).not.toHaveBeenCalled();
  });

  it("renews and replays an artifact download", async () => {
    const { fetcher, calls } = expiringBackend();
    const { api, onSessionExpired } = apiWithExpiredSession(fetcher);

    const response = await api.downloadExecutionArtifact("artifact-1");

    expect(await response.text()).toBe("artifact bytes");
    expect(calls("/session/handoff")).toHaveLength(1);
    expect(bearer(calls("/artifacts/artifact-1")[1]?.[1])).toBe("Bearer renewed-1");
    expect(onSessionExpired).not.toHaveBeenCalled();
  });
});

describe("CortexApi failure kinds", () => {
  afterEach(() => window.sessionStorage.clear());

  const jsonResponse = (body: unknown, status: number) => new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

  it("classifies an HTTP error by what the backend said", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ detail: "Local session expired." }, 401))
      .mockResolvedValueOnce(jsonResponse({ detail: [{ loc: ["body", "name"], msg: "Field required" }] }, 422))
      .mockResolvedValueOnce(jsonResponse({ detail: "Chat not found." }, 404));
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.health()).rejects.toMatchObject({ status: 401, kind: "auth" });
    await expect(api.health()).rejects.toMatchObject({ status: 422, kind: "validation" });
    await expect(api.health()).rejects.toMatchObject({ status: 404, kind: "http" });
  });

  it("carries the failure class a route names beside its sentence, and none otherwise", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ detail: { message: "Hugging Face wants a token.", code: "gated" } }, 400))
      .mockResolvedValueOnce(jsonResponse({ detail: { message: "No code here." } }, 400))
      .mockResolvedValueOnce(jsonResponse({ detail: { message: "Not text.", code: 7 } }, 400))
      .mockResolvedValueOnce(jsonResponse({ detail: "A plain sentence." }, 400));
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.health()).rejects.toMatchObject({ status: 400, detail: "Hugging Face wants a token.", code: "gated" });
    await expect(api.health()).rejects.toMatchObject({ detail: "No code here.", code: null });
    await expect(api.health()).rejects.toMatchObject({ detail: "Not text.", code: null });
    await expect(api.health()).rejects.toMatchObject({ detail: "A plain sentence.", code: null });
  });

  it("lists a repository's files with their sizes, and can abandon the request", async () => {
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const fetcher = vi.fn<typeof fetch>((_input, init) => new Promise<Response>((resolve, reject) => {
      if (String(_input).includes("owner%2Fslow")) {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        return;
      }
      resolve(jsonResponse({ repo_id: "owner/name", files: ["a.gguf"], entries: [{ path: "a.gguf", size: 12 }] }, 200));
    }));
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.listHuggingFaceGGUFFiles("owner/name")).resolves.toEqual({
      repo_id: "owner/name",
      files: ["a.gguf"],
      entries: [{ path: "a.gguf", size: 12 }],
    });
    expect(String(fetcher.mock.calls[0]?.[0])).toBe("/api/v1/models/gguf/huggingface-files?repo_id=owner%2Fname");

    const controller = new AbortController();
    const pending = api.listHuggingFaceGGUFFiles("owner/slow", { signal: controller.signal }).catch((error: unknown) => error);
    controller.abort();
    expect(isAbortedError(await pending)).toBe(true);
  });

  it("wraps a fetch network failure as a network ApiError instead of leaking the TypeError", async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new TypeError("Failed to fetch"));
    const api = new CortexApi("/api/v1", fetcher);

    const failure = await api.health().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ status: 0, kind: "network" });
    expect((failure as ApiError).detail).toMatch(/could not reach the local backend/i);
    // The browser's own wording must not be what the user reads.
    expect((failure as ApiError).detail).not.toMatch(/failed to fetch/i);
  });

  it("classifies a network failure on an authenticated request and on a streamed one", async () => {
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new TypeError("Failed to fetch"));
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.system()).rejects.toMatchObject({ kind: "network" });
    await expect(api.streamGeneration("job-1", vi.fn())).rejects.toMatchObject({ kind: "network" });
    // A dead backend is not an expired session: the session must survive it.
    expect(api.hasSession).toBe(true);
  });

  it("classifies a connection that drops in the middle of an event stream", async () => {
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('id: 1\ndata: {"event_id":1}\n\n'));
      },
      pull() {
        throw new TypeError("network error");
      },
    });
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { status: 200 }));
    const api = new CortexApi("/api/v1", fetcher);
    const events: unknown[] = [];

    await expect(api.streamGeneration("job-1", (event) => events.push(event))).rejects.toMatchObject({
      status: 0,
      kind: "network",
    });
    expect(events).toHaveLength(1);
  });

  it("aborts an in-flight request and reports it as aborted, not as a network failure", async () => {
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const fetcher = vi.fn<typeof fetch>((_input, init) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    }));
    const api = new CortexApi("/api/v1", fetcher);
    const controller = new AbortController();

    const pending = api.generationStatus("job-1", { signal: controller.signal }).catch((error: unknown) => error);
    controller.abort();
    const failure = await pending;

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ status: 0, kind: "aborted" });
    expect(isAbortedError(failure)).toBe(true);
    expect(fetcher).toHaveBeenCalledTimes(1);
    // The abort went to fetch itself; the request is not left running.
    expect(new Headers((fetcher.mock.calls[0]?.[1] as RequestInit).headers).get("Authorization")).toBe("Bearer session-1");
    expect((fetcher.mock.calls[0]?.[1] as RequestInit).signal).toBe(controller.signal);
    // Cancelling is not an expired session.
    expect(api.hasSession).toBe(true);
  });

  it("reports an abort as aborted even when the fetch implementation rejects with something else", async () => {
    const controller = new AbortController();
    const fetcher = vi.fn<typeof fetch>(async () => {
      controller.abort();
      throw new TypeError("Failed to fetch");
    });
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.generationStatus("job-1", { signal: controller.signal })).rejects.toMatchObject({ kind: "aborted" });
  });

  it("does not call an AbortError the caller did not ask for a cancellation", async () => {
    // `aborted` means the caller cancelled. An AbortError while the caller's own
    // signal is still live, or with no signal at all, came from somewhere else
    // and is a request that got no answer, like any other lost connection.
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new DOMException("Aborted", "AbortError"));
    const api = new CortexApi("/api/v1", fetcher);
    const controller = new AbortController();

    const withLiveSignal = await api.generationStatus("job-1", { signal: controller.signal }).catch((error: unknown) => error);
    const withoutSignal = await api.health().catch((error: unknown) => error);

    expect(controller.signal.aborted).toBe(false);
    for (const failure of [withLiveSignal, withoutSignal]) {
      expect(failure).toBeInstanceOf(ApiError);
      expect(failure).toMatchObject({ status: 0, kind: "network" });
      expect(isAbortedError(failure)).toBe(false);
    }
  });

  it("does not call an AbortError in the middle of an event stream a cancellation unless the caller aborted", async () => {
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const body = new ReadableStream<Uint8Array>({
      pull() {
        throw new DOMException("Aborted", "AbortError");
      },
    });
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { status: 200 }));
    const api = new CortexApi("/api/v1", fetcher);
    const controller = new AbortController();

    await expect(api.streamGeneration("job-1", vi.fn(), { signal: controller.signal })).rejects.toMatchObject({
      status: 0,
      kind: "network",
    });
  });

  it("does not disguise a non-transport failure as a network error", async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new RangeError("not a transport problem"));
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.health()).rejects.toBeInstanceOf(RangeError);
  });

  it("still renews an expired session in place and replays the request", async () => {
    window.sessionStorage.setItem("cortex.session.token", "session-old");
    let systemCalls = 0;
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/session/handoff")) return jsonResponse({ bootstrap_token: "fresh", expires_at: "2026-07-20T00:00:00Z" }, 200);
      if (url.endsWith("/session/exchange")) return jsonResponse({ session_token: "session-new", expires_at: "2026-07-20T00:00:00Z" }, 200);
      systemCalls += 1;
      return systemCalls === 1 ? jsonResponse({ detail: "Local session expired." }, 401) : jsonResponse({ status: "ok" }, 200);
    });
    const api = new CortexApi("/api/v1", fetcher);
    api.setHandoffSecret("desktop-handoff");

    await expect(api.system()).resolves.toMatchObject({ status: "ok" });
    expect(api.hasSession).toBe(true);
  });

  it("describes only what is safe to show and decides retries from the kind", () => {
    expect(describeApiError(new ApiError(404, "Chat not found."), "fallback")).toBe("Chat not found.");
    expect(describeApiError(new ApiError(0, "Cortex could not reach the local backend.", "network"), "fallback"))
      .toMatch(/could not reach/i);
    expect(describeApiError(new TypeError("Failed to fetch"), "fallback")).toBe("fallback");
    expect(describeApiError("something", "fallback")).toBe("fallback");

    // A client-side rejection spends the idempotency key; anything that leaves
    // the outcome unknown must keep it so a replay stays safe.
    expect(isDefinitiveRejection(new ApiError(422, "bad"))).toBe(true);
    expect(isDefinitiveRejection(new ApiError(409, "conflict"))).toBe(true);
    expect(isDefinitiveRejection(new ApiError(401, "expired"))).toBe(true);
    expect(isDefinitiveRejection(new ApiError(503, "unavailable"))).toBe(false);
    expect(isDefinitiveRejection(new ApiError(0, "down", "network"))).toBe(false);
    expect(isDefinitiveRejection(new ApiError(0, "cancelled", "aborted"))).toBe(false);
    expect(isDefinitiveRejection(new Error("boom"))).toBe(false);
    expect(isAbortedError(new ApiError(0, "down", "network"))).toBe(false);
    expect(isAbortedError(new Error("boom"))).toBe(false);
  });
});
