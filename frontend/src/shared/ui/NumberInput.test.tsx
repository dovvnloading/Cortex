import { createEvent, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { NumberInput } from "./NumberInput";

describe("NumberInput", () => {
  it("steps with the arrow keys and never leaves the bounds", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<NumberInput aria-label="Amount" value={1.95} min={0} max={2} decimals={2} step={0.1} onCommit={onCommit} />);

    await user.click(screen.getByRole("textbox", { name: "Amount" }));
    await user.keyboard("{ArrowUp}");
    expect(onCommit).toHaveBeenLastCalledWith(2);
    await user.keyboard("{ArrowDown}");
    expect(onCommit).toHaveBeenLastCalledWith(1.85);
  });

  it("reads a comma decimal point and grouped integers", async () => {
    const user = userEvent.setup();
    const onDecimal = vi.fn();
    const onInteger = vi.fn();
    render(
      <>
        <NumberInput aria-label="Decimal" value={0.5} min={0} max={2} decimals={2} onCommit={onDecimal} />
        <NumberInput aria-label="Integer" value={4096} min={2048} max={65536} onCommit={onInteger} />
      </>,
    );

    await user.clear(screen.getByRole("textbox", { name: "Decimal" }));
    await user.type(screen.getByRole("textbox", { name: "Decimal" }), "0,75");
    expect(onDecimal).toHaveBeenLastCalledWith(0.75);

    await user.clear(screen.getByRole("textbox", { name: "Integer" }));
    await user.type(screen.getByRole("textbox", { name: "Integer" }), "12,288");
    expect(onInteger).toHaveBeenLastCalledWith(12288);
  });

  it.each([
    ["en-US", "16,384"],
    ["de-DE", "16.384"],
    ["fr-FR", "16\u202f384"],
    ["fr-FR with NBSP", "16\u00a0384"],
  ])("parses localized integer grouping on edit and blur: %s", (_locale, formatted) => {
    const onCommit = vi.fn();
    render(<NumberInput aria-label="Context window" value={8192} min={2048} max={65536} onCommit={onCommit} />);
    const input = screen.getByRole("textbox", { name: "Context window" });
    fireEvent.change(input, { target: { value: formatted } });
    fireEvent.blur(input);
    expect(onCommit).toHaveBeenLastCalledWith(16384);
  });

  it("does not mistake decimal-looking text for an integer and clamp it to 2048", () => {
    const onCommit = vi.fn();
    render(<NumberInput aria-label="Context window" value={8192} min={2048} max={65536} onCommit={onCommit} />);
    const input = screen.getByRole("textbox", { name: "Context window" });
    fireEvent.change(input, { target: { value: "1.5" } });
    fireEvent.blur(input);
    expect(onCommit).not.toHaveBeenCalled();
    expect(input).toHaveValue("8192");
  });

  it("steps from a period-grouped integer rather than the decimal 8.192", () => {
    const onCommit = vi.fn();
    render(<NumberInput aria-label="Context window" value={8192} min={2048} max={65536} step={1024} onCommit={onCommit} />);
    const input = screen.getByRole("textbox", { name: "Context window" });
    fireEvent.change(input, { target: { value: "8.192" } });
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(onCommit).toHaveBeenLastCalledWith(9216);
  });

  it("leaves composing Enter and arrow keys to the IME without mutating numeric settings", () => {
    const onCommit = vi.fn();
    render(<NumberInput aria-label="Amount" value={10} min={0} max={200} step={1} onCommit={onCommit} />);
    const input = screen.getByRole("textbox", { name: "Amount" });

    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "12" } });
    expect(input).toHaveValue("12");
    expect(onCommit).not.toHaveBeenCalled();

    for (const key of ["Enter", "ArrowUp", "ArrowDown"]) {
      const event = createEvent.keyDown(input, { key, code: key, isComposing: true });
      fireEvent(input, event);
      expect(event.defaultPrevented).toBe(false);
      expect(onCommit).not.toHaveBeenCalled();
      expect(input).toHaveValue("12");
    }
    fireEvent.compositionEnd(input);
    expect(onCommit).toHaveBeenCalledExactlyOnceWith(12);

    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(onCommit).toHaveBeenLastCalledWith(13);
  });

  it("ignores native composing events even before compositionstart has been observed", () => {
    const onCommit = vi.fn();
    render(<NumberInput aria-label="Amount" value={10} min={0} max={200} onCommit={onCommit} />);
    const input = screen.getByRole("textbox", { name: "Amount" });
    const event = createEvent.keyDown(input, { key: "ArrowDown", code: "ArrowDown", isComposing: true });
    fireEvent(input, event);
    expect(event.defaultPrevented).toBe(false);
    expect(onCommit).not.toHaveBeenCalled();
    expect(input).toHaveValue("10");
  });

  it("restores the last good value when the text is unusable", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<NumberInput aria-label="Amount" value={40} min={0} max={200} onCommit={onCommit} />);

    const input = screen.getByRole("textbox", { name: "Amount" });
    await user.clear(input);
    await user.type(input, "abc");
    await user.keyboard("{Enter}");

    expect(onCommit).not.toHaveBeenCalled();
    expect(input).toHaveValue("40");
  });
});
