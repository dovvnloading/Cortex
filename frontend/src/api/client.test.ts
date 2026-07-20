import { describe, expect, it, vi } from "vitest";
import { ApiError, CortexApi } from "./client";

describe("CortexApi", () => {
  it("exchanges a bootstrap token and sends the session bearer on protected calls", async () => {
    const fetcher = vi.fn<typeof fetch>();
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ session_token: "session-1", expires_at: "2026-07-20T00:00:00Z" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ status: "ok", preview: true, qt_default: true, started_at: "2026-07-20T00:00:00Z" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const api = new CortexApi("/api/v1", fetcher);

    await api.exchangeBootstrapToken("bootstrap");
    await api.system();

    expect(fetcher).toHaveBeenNthCalledWith(1, "/api/v1/session/exchange", expect.objectContaining({ method: "POST" }));
    expect(fetcher).toHaveBeenNthCalledWith(2, "/api/v1/system", expect.objectContaining({ headers: expect.any(Headers) }));
    const secondRequest = fetcher.mock.calls[1]?.[1] as RequestInit;
    expect(new Headers(secondRequest.headers).get("Authorization")).toBe("Bearer session-1");
    expect(api.hasSession).toBe(true);
  });

  it("turns safe API errors into ApiError without assuming a response body", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response("", { status: 503 }));
    const api = new CortexApi("/api/v1", fetcher);

    await expect(api.health()).rejects.toEqual(new ApiError(503, "Cortex API request failed."));
  });

  it("parses ordered authenticated generation events from an SSE response", async () => {
    const sse = [
      'id: 1\nevent: generation.queued\ndata: {"event_id":1,"event":"generation.queued","job_id":"job-1","thread_id":"thread-1","data":{}}\n\n',
      'id: 2\nevent: generation.content_delta\ndata: {"event_id":2,"event":"generation.content_delta","job_id":"job-1","thread_id":"thread-1","data":{"delta":"hello"}}\n\n',
    ].join("");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(sse, { status: 200 }));
    const api = new CortexApi("/api/v1", fetcher);
    window.sessionStorage.setItem("cortex.session.token", "session-1");
    const events: string[] = [];

    await api.streamGeneration("job-1", (event) => events.push(event.event), { afterEventId: 0 });

    expect(events).toEqual(["generation.queued", "generation.content_delta"]);
    const request = fetcher.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).get("Authorization")).toBe("Bearer session-1");
    expect(new Headers(request.headers).get("Last-Event-ID")).toBe("0");
  });
});
