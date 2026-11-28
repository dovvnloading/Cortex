import { describe, expect, it } from "vitest";
import { agentModelNames, chatModelNames, localModelNames } from "./localModels";

describe("agentModelNames", () => {
  const inventory = {
    installed_models: ["qwen3:8b", "nomic-embed-text:latest", "legacy:1b"],
    models: [
      { name: "qwen3:8b", capabilities: ["completion", "tools", "thinking"] },
      { name: "nomic-embed-text:latest", capabilities: ["embedding"] },
      { name: "translategemma:4b", capabilities: ["completion", "vision"] },
      { name: "gguf:local.gguf", source: "gguf" as const },
      { name: "unreported:2b", capabilities: [] },
    ],
  };

  it("drops models whose reported capabilities rule out chat", () => {
    expect(agentModelNames(inventory)).toEqual([
      "gguf:local.gguf", "legacy:1b", "qwen3:8b", "translategemma:4b", "unreported:2b",
    ]);
  });

  it("keeps the full inventory for everything else", () => {
    expect(localModelNames(inventory)).toContain("nomic-embed-text:latest");
  });

  it("does not hide a model that only the compact list reports", () => {
    expect(agentModelNames({ installed_models: ["nomic-embed-text:latest"] })).toEqual(["nomic-embed-text:latest"]);
  });
});

describe("chatModelNames", () => {
  it("never offers an embedding-only model for chat (#719)", () => {
    const inventory = {
      installed_models: ["qwen3:8b", "nomic-embed-text:latest"],
      models: [
        { name: "qwen3:8b", capabilities: ["completion"] },
        { name: "nomic-embed-text:latest", capabilities: ["embedding"] },
        { name: "gguf:local.gguf", source: "gguf" as const },
      ],
    };
    expect(chatModelNames(inventory)).toEqual(["gguf:local.gguf", "qwen3:8b"]);
    expect(agentModelNames(inventory)).toEqual(chatModelNames(inventory));
  });
});
