import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, CortexApi } from "./client";

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

    await expect(pending).rejects.toEqual(new ApiError(401, "Local session expired."));
    expect(onSessionExpired).not.toHaveBeenCalled();
    expect(api.hasSession).toBe(true);
    expect(window.sessionStorage.getItem("cortex.session.token")).toBe("session-2");
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

    await expect(pending).rejects.toEqual(new ApiError(401, "Local session expired."));
    expect(onSessionExpired).not.toHaveBeenCalled();
    expect(api.hasSession).toBe(true);
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

  it("starts a typed recipe request on the explicit qualification route", async () => {
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

  it("stages a bounded attachment through the qualification route", async () => {
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
