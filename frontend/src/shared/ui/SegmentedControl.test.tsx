import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SegmentedControl } from "./SegmentedControl";

const OPTIONS = [
  { value: "a", label: "A" },
  { value: "b", label: "B" },
  { value: "c", label: "C" },
] as const;

describe("SegmentedControl", () => {
  it("is one tab stop and moves the choice with arrow keys, wrapping at the ends", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SegmentedControl aria-label="Letters" options={OPTIONS} value="c" onChange={onChange} />);

    const group = screen.getByRole("radiogroup", { name: "Letters" });
    expect(screen.getByRole("radio", { name: "C" })).toBeChecked();
    expect(screen.getAllByRole("radio").map((radio) => radio.tabIndex)).toEqual([-1, -1, 0]);

    await user.tab();
    expect(screen.getByRole("radio", { name: "C" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(onChange).toHaveBeenLastCalledWith("a");
    expect(screen.getByRole("radio", { name: "A" })).toHaveFocus();
    await user.keyboard("{End}");
    expect(onChange).toHaveBeenLastCalledWith("c");
    expect(group).toBeVisible();
  });

  it("allows no segment to be checked while keeping the first reachable", () => {
    render(<SegmentedControl aria-label="Letters" options={OPTIONS} value={null} onChange={vi.fn()} />);

    expect(screen.queryByRole("radio", { checked: true })).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "A" })).toHaveAttribute("tabindex", "0");
  });
});
