import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SafeMarkdown } from "./SafeMarkdown";

// The native window now shows a Save As dialog for any download, so nothing a
// model writes may be able to start one.
describe("SafeMarkdown downloads", () => {
  it("renders an <a download> written in Markdown without the attribute", () => {
    render(<SafeMarkdown content={'<a href="https://example.com/setup.exe" download="setup.exe">get it</a>'} />);

    expect(document.querySelector("[download]")).toBeNull();
    // Raw HTML is not turned into elements at all; only its text survives.
    expect(document.querySelector("a")).toBeNull();
    expect(document.body).toHaveTextContent("get it");
  });

  it("gives a Markdown link no download attribute and opens it outside the window", () => {
    render(<SafeMarkdown content={"[report](https://example.com/report.pdf)"} />);

    const link = screen.getByRole("link", { name: "report" });
    expect(link).not.toHaveAttribute("download");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("does not link a data: or blob: URL that a download could be built from", () => {
    render(<SafeMarkdown content={"[a](data:text/plain;base64,aGk=) [b](blob:https://example.com/id)"} />);

    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
