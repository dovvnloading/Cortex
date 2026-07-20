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
});
