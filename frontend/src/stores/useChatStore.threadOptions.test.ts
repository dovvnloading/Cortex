import { afterEach, describe, expect, it } from "vitest";
import { readPersistedGenerationOptions } from "../lib/persistedGenerationOptions";
import { useChatStore } from "./useChatStore";

describe("useChatStore thread options", () => {
  afterEach(() => {
    window.localStorage.clear();
  });

  it("prunes deleted chats' options from memory too, so a later change cannot write them back (#689)", () => {
    useChatStore.setState({ generationOptionsByThread: {} });
    useChatStore.getState().setThreadOptions("kept-chat", { temperature: 0.3 });
    for (let index = 0; index < 99; index += 1) {
      useChatStore.getState().setThreadOptions(`gone-${index}`, { seed: index });
    }
    useChatStore.getState().pruneThreadOptions(["kept-chat", "new-chat"]);
    useChatStore.getState().setThreadOptions("new-chat", { temperature: 0.9 });

    expect(Object.keys(useChatStore.getState().generationOptionsByThread).sort()).toEqual(["kept-chat", "new-chat"]);
    expect(readPersistedGenerationOptions()).toEqual({
      "kept-chat": { temperature: 0.3 }, "new-chat": { temperature: 0.9 },
    });
  });
});
