import { render, screen } from "@testing-library/react";
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
