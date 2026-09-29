// @vitest-environment node
import { beforeEach, describe, expect, it } from "vitest";
import { useChatStore } from "./useChatStore";

const idleGeneration = {
  jobId: null,
  threadId: null,
  phase: "idle" as const,
  partialContent: "",
  partialThoughts: "",
  statusText: "",
  contentReady: false,
  gap: false,
};

describe("useChatStore", () => {
  beforeEach(() => {
    useChatStore.setState({ chats: [], generation: idleGeneration, generationCursor: 0, generationOptionsByThread: {} });
  });

  it("setChats accepts a plain array, mirroring useState", () => {
    useChatStore.getState().setChats([{ id: "a", title: "Alpha", timestamp: "t" }]);
    expect(useChatStore.getState().chats).toEqual([{ id: "a", title: "Alpha", timestamp: "t" }]);
  });

  it("setChats accepts an updater function, mirroring useState", () => {
    useChatStore.getState().setChats([{ id: "a", title: "Alpha", timestamp: "t" }]);
    useChatStore.getState().setChats((current) => current.filter((chat) => chat.id !== "a"));
    expect(useChatStore.getState().chats).toEqual([]);
  });

  it("upsertChatSummary prepends a new chat and dedupes an existing id", () => {
    useChatStore.getState().setChats([{ id: "a", title: "Alpha", timestamp: "t1", group_id: "g1" }]);
    useChatStore.getState().upsertChatSummary({ id: "b", title: "Beta", timestamp: "t2", messages: [] });
    expect(useChatStore.getState().chats).toEqual([
      { id: "b", title: "Beta", timestamp: "t2", group_id: null },
      { id: "a", title: "Alpha", timestamp: "t1", group_id: "g1" },
    ]);

    // Re-upserting an existing chat must not evict it from its group: the
    // response that triggers this carries group_id only once the server has
    // it, so the known filing has to win over an absent field.
    useChatStore.getState().upsertChatSummary({ id: "a", title: "Alpha renamed", timestamp: "t3", messages: [] });
    expect(useChatStore.getState().chats).toEqual([
      { id: "a", title: "Alpha renamed", timestamp: "t3", group_id: "g1" },
      { id: "b", title: "Beta", timestamp: "t2", group_id: null },
    ]);
  });

  it("beginGeneration moves the generation slice to starting for the given job", () => {
    useChatStore.getState().beginGeneration("job-1", "thread-1");
    const generation = useChatStore.getState().generation;
    expect(generation).toMatchObject({ jobId: "job-1", threadId: "thread-1", phase: "starting", partialContent: "" });
  });

  it("appendContentToken accumulates deltas for the active job and flips phase to streaming", () => {
    useChatStore.getState().beginGeneration("job-1", "thread-1");
    useChatStore.getState().appendContentToken("job-1", "Hel");
    useChatStore.getState().appendContentToken("job-1", "lo");
    const generation = useChatStore.getState().generation;
    expect(generation.partialContent).toBe("Hello");
    expect(generation.phase).toBe("streaming");
  });

  it("keeps the latest stream cursor for storage-independent recovery", () => {
    useChatStore.getState().beginGeneration("job-cursor", "thread-cursor");
    useChatStore.getState().setGenerationCursor("job-cursor", 7);
    useChatStore.getState().setGenerationCursor("job-cursor", 3);

    expect(useChatStore.getState().generationCursor).toBe(7);
  });

  it("ignores content/thinking tokens for a stale or unrelated jobId", () => {
    useChatStore.getState().beginGeneration("job-1", "thread-1");
    useChatStore.getState().appendContentToken("stale-job", "should not appear");
    useChatStore.getState().appendThinkingToken("stale-job", "should not appear either");
    const generation = useChatStore.getState().generation;
    expect(generation.partialContent).toBe("");
    expect(generation.partialThoughts).toBe("");
    expect(generation.jobId).toBe("job-1");
  });

  it("markStopping/revertStopping only affect the matching job", () => {
    useChatStore.getState().beginGeneration("job-1", "thread-1");
    useChatStore.getState().markStopping("stale-job");
    expect(useChatStore.getState().generation.phase).toBe("starting");

    useChatStore.getState().markStopping("job-1");
    expect(useChatStore.getState().generation.phase).toBe("stopping");

    useChatStore.getState().revertStopping("stale-job");
    expect(useChatStore.getState().generation.phase).toBe("stopping");

    useChatStore.getState().revertStopping("job-1");
    expect(useChatStore.getState().generation.phase).toBe("streaming");
  });

  it("markContentReady flips contentReady only for the matching job, without touching the buffered text", () => {
    useChatStore.getState().beginGeneration("job-1", "thread-1");
    useChatStore.getState().appendContentToken("job-1", "The answer");
    useChatStore.getState().markContentReady("stale-job");
    expect(useChatStore.getState().generation.contentReady).toBe(false);

    useChatStore.getState().markContentReady("job-1");
    expect(useChatStore.getState().generation.contentReady).toBe(true);
    expect(useChatStore.getState().generation.partialContent).toBe("The answer");
  });

  it("endGeneration resets the slice to idle only for the matching job", () => {
    useChatStore.getState().beginGeneration("job-1", "thread-1");
    useChatStore.getState().appendContentToken("job-1", "partial");

    useChatStore.getState().endGeneration("some-other-job");
    expect(useChatStore.getState().generation.jobId).toBe("job-1");
    expect(useChatStore.getState().generation.partialContent).toBe("partial");

    useChatStore.getState().endGeneration("job-1");
    expect(useChatStore.getState().generation).toEqual(idleGeneration);
  });

  it("setThreadOptions scopes overrides per thread key and clears with null", () => {
    useChatStore.getState().setThreadOptions("thread-a", { temperature: 0.2 });
    useChatStore.getState().setThreadOptions("thread-b", { top_p: 0.5 });
    expect(useChatStore.getState().generationOptionsByThread).toEqual({
      "thread-a": { temperature: 0.2 },
      "thread-b": { top_p: 0.5 },
    });

    useChatStore.getState().setThreadOptions("thread-a", null);
    expect(useChatStore.getState().generationOptionsByThread).toEqual({
      "thread-b": { top_p: 0.5 },
    });
  });

  // ChatPage subscribes to the whole `generation` object by reference, and the
  // writes below run on every SSE frame. Rebuilding that object when nothing
  // it renders has changed re-rendered the transcript per frame and cancelled
  // out the rAF batching in useGenerationStream. Identity is the assertion --
  // deep equality would pass either way.
  it("does not touch the generation slice when the event cursor advances", () => {
    useChatStore.getState().beginGeneration("job-cursor-identity", "thread-cursor-identity");
    const before = useChatStore.getState().generation;

    for (let eventId = 1; eventId <= 25; eventId += 1) {
      useChatStore.getState().setGenerationCursor("job-cursor-identity", eventId);
    }

    expect(useChatStore.getState().generationCursor).toBe(25);
    expect(useChatStore.getState().generation).toBe(before);
  });

  it("leaves the generation slice untouched when a per-frame write changes nothing", () => {
    useChatStore.getState().beginGeneration("job-identity", "thread-identity");
    useChatStore.getState().setStatusText("job-identity", "Response content available.");
    useChatStore.getState().markContentReady("job-identity");
    const before = useChatStore.getState().generation;

    // The same status message rides every content_delta frame.
    useChatStore.getState().setStatusText("job-identity", "Response content available.");
    expect(useChatStore.getState().generation).toBe(before);

    // Already ready, and already stopping.
    useChatStore.getState().markContentReady("job-identity");
    expect(useChatStore.getState().generation).toBe(before);
    useChatStore.getState().markStopping("job-identity");
    const stopping = useChatStore.getState().generation;
    expect(stopping).not.toBe(before);
    useChatStore.getState().markStopping("job-identity");
    expect(useChatStore.getState().generation).toBe(stopping);
  });

  it("replaceContent swaps the whole streamed answer, and only for the active job", () => {
    useChatStore.getState().beginGeneration("job-replace", "thread-replace");
    useChatStore.getState().appendContentToken("job-replace", "Hola mundo");

    useChatStore.getState().replaceContent("job-replace", "Hello world");
    expect(useChatStore.getState().generation.partialContent).toBe("Hello world");

    // A stale job's replacement must not overwrite the answer now on screen.
    useChatStore.getState().replaceContent("some-other-job", "wrong");
    expect(useChatStore.getState().generation.partialContent).toBe("Hello world");

    // Replacing with what is already there is not a change, so it must not
    // rebuild the slice the transcript subscribes to.
    const before = useChatStore.getState().generation;
    useChatStore.getState().replaceContent("job-replace", "Hello world");
    expect(useChatStore.getState().generation).toBe(before);
  });

  it("still applies a per-frame write that does change something", () => {
    useChatStore.getState().beginGeneration("job-advance", "thread-advance");
    const initial = useChatStore.getState().generation;

    useChatStore.getState().setStatusText("job-advance", "Saving the response.");
    expect(useChatStore.getState().generation).not.toBe(initial);
    expect(useChatStore.getState().generation.statusText).toBe("Saving the response.");
  });

  it("resets the event cursor with the generation it belongs to", () => {
    useChatStore.getState().beginGeneration("job-reset-cursor", "thread-reset-cursor");
    useChatStore.getState().setGenerationCursor("job-reset-cursor", 9);
    expect(useChatStore.getState().generationCursor).toBe(9);

    useChatStore.getState().endGeneration("job-reset-cursor");
    expect(useChatStore.getState().generationCursor).toBe(0);
  });

  it("markGenerationGap flags only the matching job, once, and says why in the status line", () => {
    useChatStore.getState().beginGeneration("job-gap", "thread-gap");
    useChatStore.getState().appendContentToken("job-gap", "kept");

    useChatStore.getState().markGenerationGap("some-other-job");
    expect(useChatStore.getState().generation.gap).toBe(false);

    useChatStore.getState().markGenerationGap("job-gap");
    const flagged = useChatStore.getState().generation;
    expect(flagged).toMatchObject({ gap: true, partialContent: "kept" });
    expect(flagged.statusText).toMatch(/full answer will appear when it is saved/i);

    // Flagging again is a no-op, so a reconnect does not rebuild the slice.
    useChatStore.getState().markGenerationGap("job-gap");
    expect(useChatStore.getState().generation).toBe(flagged);

    // A new job starts without it.
    useChatStore.getState().endGeneration("job-gap");
    useChatStore.getState().beginGeneration("job-next", "thread-next");
    expect(useChatStore.getState().generation.gap).toBe(false);
  });

  it("holds the newest outcome until someone acknowledges it, and only that one", () => {
    const { recordCompletion, acknowledgeCompletion, recordFailure, acknowledgeFailure } = useChatStore.getState();

    recordCompletion({ jobId: "job-1", threadId: "thread-1", chat: null, clearRequested: false });
    const first = useChatStore.getState().lastCompletion;
    expect(first).toMatchObject({ jobId: "job-1", threadId: "thread-1", chat: null, clearRequested: false });

    // A newer outcome replaces it and gets a larger id, so a reader can tell
    // a new outcome from one it has already handled.
    recordCompletion({ jobId: "job-2", threadId: "thread-2", chat: null, clearRequested: true });
    const second = useChatStore.getState().lastCompletion;
    expect(second?.id).toBeGreaterThan(first?.id ?? 0);

    // Acknowledging a superseded outcome must not discard the newer one.
    acknowledgeCompletion(first?.id ?? -1);
    expect(useChatStore.getState().lastCompletion).toBe(second);
    acknowledgeCompletion(second?.id ?? -1);
    expect(useChatStore.getState().lastCompletion).toBeNull();

    recordFailure({ threadId: "thread-1", message: "Generation did not complete." });
    const failure = useChatStore.getState().lastFailure;
    expect(failure).toMatchObject({ threadId: "thread-1", message: "Generation did not complete." });
    acknowledgeFailure((failure?.id ?? 0) + 1);
    expect(useChatStore.getState().lastFailure).toBe(failure);
    acknowledgeFailure(failure?.id ?? -1);
    expect(useChatStore.getState().lastFailure).toBeNull();
  });

  it("setThreadOptions replaces (not merges) an existing entry for the same key", () => {
    useChatStore.getState().setThreadOptions("thread-a", { temperature: 0.2, top_p: 0.5 });
    useChatStore.getState().setThreadOptions("thread-a", { temperature: 0.9 });
    expect(useChatStore.getState().generationOptionsByThread["thread-a"]).toEqual({ temperature: 0.9 });
  });

  describe("suggested memories", () => {
    beforeEach(() => {
      useChatStore.setState({ proposedMemoriesByMessage: {} });
    });

    it("keeps a message's suggestions and replaces them wholesale", () => {
      useChatStore.getState().setProposedMemories("m-1", ["Likes tea.", "Lives in Oslo."]);
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({ "m-1": ["Likes tea.", "Lives in Oslo."] });

      useChatStore.getState().setProposedMemories("m-1", ["Likes coffee."]);
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({ "m-1": ["Likes coffee."] });
    });

    it("forgets a message's suggestions when the new list is empty", () => {
      useChatStore.getState().setProposedMemories("m-1", ["Likes tea."]);
      useChatStore.getState().setProposedMemories("m-2", ["Lives in Oslo."]);

      useChatStore.getState().setProposedMemories("m-1", []);

      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({ "m-2": ["Lives in Oslo."] });
    });

    it("does not rebuild state when nothing changed", () => {
      useChatStore.getState().setProposedMemories("m-1", ["Likes tea."]);
      const before = useChatStore.getState().proposedMemoriesByMessage;

      useChatStore.getState().setProposedMemories("m-1", ["Likes tea."]);
      useChatStore.getState().setProposedMemories("m-none", []);
      useChatStore.getState().dismissProposedMemory("m-1", "Not a suggestion.");

      expect(useChatStore.getState().proposedMemoriesByMessage).toBe(before);
    });

    it("dismisses one suggestion at a time and drops the message when none remain", () => {
      useChatStore.getState().setProposedMemories("m-1", ["Likes tea.", "Lives in Oslo."]);

      useChatStore.getState().dismissProposedMemory("m-1", "Likes tea.");
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({ "m-1": ["Lives in Oslo."] });

      useChatStore.getState().dismissProposedMemory("m-1", "Lives in Oslo.");
      expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({});
    });
  });
});
