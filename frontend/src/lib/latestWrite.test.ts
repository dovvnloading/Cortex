import { describe, expect, it, vi } from "vitest";
import { persistLatestWrite, type LatestWrite } from "./latestWrite";

function deferred() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => { release = resolve; });
  return { promise, release };
}

describe("persistLatestWrite", () => {
  it("never starts a newer write before the earlier one settles", async () => {
    const first = deferred();
    const pending = new Map<string, LatestWrite<string>>();
    const requested: string[] = [];
    const saved: string[] = [];
    const onFailure = vi.fn();
    const persist = async (value: string) => {
      requested.push(value);
      if (value === "A") await first.promise;
      saved.push(value);
    };

    persistLatestWrite(pending, "thread", "none", "A", persist, onFailure);
    persistLatestWrite(pending, "thread", "A", "B", persist, onFailure);
    expect(requested).toEqual(["A"]);
    expect(pending.get("thread")?.desired).toBe("B");

    first.release();
    await vi.waitFor(() => expect(saved).toEqual(["A", "B"]));
    expect(requested).toEqual(["A", "B"]);
    await vi.waitFor(() => expect(pending.size).toBe(0));
    expect(onFailure).not.toHaveBeenCalled();
  });

  it("compensates when the latest intent returns to the original value", async () => {
    const first = deferred();
    const requested: string[] = [];
    let saved = "original";
    const pending = new Map<string, LatestWrite<string>>();
    const persist = async (value: string) => {
      requested.push(value);
      if (requested.length === 1) await first.promise;
      saved = value;
    };

    persistLatestWrite(pending, "thread", saved, "first", persist, () => undefined);
    persistLatestWrite(pending, "thread", "first", "original", persist, () => undefined);
    first.release();

    await vi.waitFor(() => expect(requested).toEqual(["first", "original"]));
    expect(saved).toBe("original");
  });

  it("still sends a newer intention after an earlier ambiguous failure", async () => {
    const first = deferred();
    const requested: string[] = [];
    let saved = "original";
    const onFailure = vi.fn();
    const pending = new Map<string, LatestWrite<string>>();
    const persist = async (value: string) => {
      requested.push(value);
      if (value === "A") {
        await first.promise;
        // Simulate a lost acknowledgement: the backend did commit A.
        saved = value;
        throw new Error("network closed");
      }
      saved = value;
    };

    persistLatestWrite(pending, "thread", saved, "A", persist, onFailure);
    persistLatestWrite(pending, "thread", "A", "original", persist, onFailure);
    first.release();

    await vi.waitFor(() => expect(requested).toEqual(["A", "original"]));
    expect(saved).toBe("original");
    expect(onFailure).not.toHaveBeenCalled();
  });

  it("rolls back only the latest failed intent and clears the pending slot", async () => {
    const pending = new Map<string, LatestWrite<boolean>>();
    const error = new Error("rejected");
    const onFailure = vi.fn();
    persistLatestWrite(
      pending, "group", false, true,
      async () => { throw error; },
      onFailure,
    );

    await vi.waitFor(() => expect(onFailure).toHaveBeenCalledWith(true, false, error));
    expect(pending.size).toBe(0);
  });
});
