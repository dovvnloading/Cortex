// @vitest-environment node
import { describe, expect, it } from "vitest";
import { groupGGUFFiles, HUGGING_FACE_REPO_ID } from "./huggingFaceFiles";

describe("groupGGUFFiles", () => {
  it("lists plain files with their sizes, top level first and then folder by folder", () => {
    const choices = groupGGUFFiles([
      { path: "weights/model.Q8_0.gguf", size: 8_000 },
      { path: "model.Q4_K_M.gguf", size: 4_000 },
      { path: "weights/model.Q2_K.gguf", size: null },
      { path: "model.Q10.gguf", size: 10_000 },
      { path: "model.Q9.gguf" },
    ]);

    expect(choices.map((choice) => [choice.directory, choice.label, choice.size])).toEqual([
      ["", "model.Q4_K_M.gguf", 4_000],
      ["", "model.Q9.gguf", null],
      ["", "model.Q10.gguf", 10_000],
      ["weights", "model.Q2_K.gguf", null],
      ["weights", "model.Q8_0.gguf", 8_000],
    ]);
    expect(choices.every((choice) => choice.parts === 1 && choice.complete)).toBe(true);
    expect(choices[0].path).toBe("model.Q4_K_M.gguf");
  });

  it("collapses a split model into one choice that names its first part", () => {
    const choices = groupGGUFFiles([
      { path: "big/model-Q4_K_M-00003-of-00003.gguf", size: 300 },
      { path: "big/model-Q4_K_M-00001-of-00003.gguf", size: 100 },
      { path: "big/model-Q4_K_M-00002-of-00003.gguf", size: 200 },
      { path: "small.gguf", size: 5 },
    ]);

    expect(choices).toEqual([
      { path: "small.gguf", directory: "", label: "small.gguf", size: 5, parts: 1, complete: true },
      {
        path: "big/model-Q4_K_M-00001-of-00003.gguf",
        directory: "big",
        label: "model-Q4_K_M.gguf",
        size: 600,
        parts: 3,
        complete: true,
      },
    ]);
  });

  it("has no total size for a split model when any part's size is unknown", () => {
    const [choice] = groupGGUFFiles([
      { path: "m-00001-of-00002.gguf", size: 100 },
      { path: "m-00002-of-00002.gguf", size: null },
    ]);
    expect(choice).toMatchObject({ parts: 2, complete: true, size: null });
  });

  it("marks a split model that lacks a part, so it is not offered as a download", () => {
    const [choice] = groupGGUFFiles([
      { path: "m-00001-of-00003.gguf", size: 100 },
      { path: "m-00003-of-00003.gguf", size: 100 },
    ]);
    expect(choice).toMatchObject({ parts: 3, complete: false, size: null, path: "m-00001-of-00003.gguf" });

    const [withoutFirst] = groupGGUFFiles([{ path: "m-00002-of-00002.gguf", size: 1 }]);
    expect(withoutFirst.complete).toBe(false);
  });

  it("keeps sets in different folders or of different sizes apart", () => {
    const choices = groupGGUFFiles([
      { path: "a/m-00001-of-00002.gguf" },
      { path: "a/m-00002-of-00002.gguf" },
      { path: "b/m-00001-of-00002.gguf" },
      { path: "b/m-00002-of-00002.gguf" },
      { path: "a/m-00001-of-00003.gguf" },
    ]);
    expect(choices.map((choice) => [choice.directory, choice.parts, choice.complete])).toEqual([
      ["a", 2, true],
      ["a", 3, false],
      ["b", 2, true],
    ]);
  });

  it("treats a name that only looks like a part as an ordinary file, as the backend does", () => {
    const choices = groupGGUFFiles([
      { path: "m-00005-of-00003.gguf" },
      { path: "m-00000-of-00003.gguf" },
      { path: "m-00001-of-00001.gguf" },
      { path: "m-1-of-2.gguf" },
    ]);
    expect(choices).toHaveLength(4);
    expect(choices.every((choice) => choice.parts === 1 && choice.complete)).toBe(true);
  });
});

describe("HUGGING_FACE_REPO_ID", () => {
  it.each(["owner/name", "bartowski/some-model-GGUF", "a.b/c_d-e"])("accepts %s", (value) => {
    expect(HUGGING_FACE_REPO_ID.test(value)).toBe(true);
  });

  it.each(["", "owner", "owner/", "/name", "a/b/c", "owner/na me", "https://huggingface.co/a/b"])("rejects %j", (value) => {
    expect(HUGGING_FACE_REPO_ID.test(value)).toBe(false);
  });
});
