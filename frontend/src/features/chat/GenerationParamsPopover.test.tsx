import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { GenerationParamsPopover } from "./GenerationParamsPopover";

const DEFAULTS = { temperature: 0.7, top_p: 0.9, top_k: 40, repeat_penalty: 1.1, num_ctx: 4096, seed: -1 };

async function openPopover(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Generation parameters for this chat" }));
  return screen.findByRole("dialog", { name: "Generation parameters" });
}

describe("GenerationParamsPopover", () => {
  it("shows the trigger without an active indicator when there is no override", () => {
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: "Generation parameters for this chat" });
    expect(trigger.className).not.toContain("icon-button-active");
  });

  it("marks the trigger active once an override is set", () => {
    render(<GenerationParamsPopover value={{ temperature: 0.2 }} defaults={DEFAULTS} onChange={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: "Generation parameters for this chat" });
    expect(trigger.className).toContain("icon-button-active");
    expect(trigger).toHaveAttribute("title", "1 parameter differs from your defaults");
  });

  it("does not count an override that only repeats a default as active", () => {
    render(<GenerationParamsPopover value={{ temperature: 0.7 }} defaults={DEFAULTS} onChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Generation parameters for this chat" }).className).not.toContain("icon-button-active");
  });

  it("opens on trigger click and shows default values pre-filled", async () => {
    const user = userEvent.setup();
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={vi.fn()} />);

    const dialog = await openPopover(user);

    expect(within(dialog).getByText("Chat parameters")).toBeVisible();
    expect(within(dialog).getByText("This chat uses your defaults.")).toBeVisible();
    expect(within(dialog).getByRole("slider", { name: "Temperature" })).toHaveValue("0.7");
    expect(within(dialog).getByRole("textbox", { name: "Temperature value" })).toHaveValue("0.70");
    expect(within(dialog).getByRole("textbox", { name: "Context window" })).toHaveValue("4,096");
    expect(within(dialog).getByRole("radio", { name: "4,096 tokens" })).toBeChecked();
    expect(within(dialog).getByRole("radio", { name: "Balanced" })).toBeChecked();
  });

  it("calls onChange with an override object when a slider moves", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={onChange} />);

    await openPopover(user);
    // userEvent.type doesn't apply to range inputs; fireEvent.change goes
    // through React's value-setter tracking correctly, unlike a raw dispatch.
    fireEvent.change(screen.getByRole("slider", { name: "Temperature" }), { target: { value: "0.2" } });

    expect(onChange).toHaveBeenLastCalledWith({ temperature: 0.2 });
  });

  it("drops an override once it is moved back to the default", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GenerationParamsPopover value={{ temperature: 0.2 }} defaults={DEFAULTS} onChange={onChange} />);

    await openPopover(user);
    fireEvent.change(screen.getByRole("slider", { name: "Temperature" }), { target: { value: "0.7" } });

    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it("accepts an exact typed value", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={onChange} />);

    await openPopover(user);
    const readout = screen.getByRole("textbox", { name: "Temperature value" });
    await user.clear(readout);
    await user.type(readout, "1.25");

    expect(onChange).toHaveBeenLastCalledWith({ temperature: 1.25 });
  });

  it("clamps an out-of-range typed value to the backend limit when the field is left", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={onChange} />);

    await openPopover(user);
    const readout = screen.getByRole("textbox", { name: "Temperature value" });
    await user.clear(readout);
    await user.type(readout, "9");
    // Nothing out of range is committed while typing...
    expect(onChange).not.toHaveBeenCalled();
    await user.tab();

    // ...and leaving the field settles on the nearest legal value.
    expect(onChange).toHaveBeenLastCalledWith({ temperature: 2 });
  });

  it("applies a preset as an override of only the fields that differ", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={onChange} />);

    await openPopover(user);
    await user.click(screen.getByRole("radio", { name: "Precise" }));

    expect(onChange).toHaveBeenLastCalledWith({ temperature: 0.2, top_p: 0.8, top_k: 20 });
  });

  it("picks a context window size from the common stops", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={onChange} />);

    await openPopover(user);
    await user.click(screen.getByRole("radio", { name: "16,384 tokens" }));

    expect(onChange).toHaveBeenLastCalledWith({ num_ctx: 16384 });
  });

  it("resets a single field without touching the others", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GenerationParamsPopover value={{ temperature: 0.2, top_p: 0.5 }} defaults={DEFAULTS} onChange={onChange} />);

    await openPopover(user);
    await user.click(screen.getByRole("button", { name: "Reset Temperature to 0.70" }));

    expect(onChange).toHaveBeenLastCalledWith({ top_p: 0.5 });
  });

  it("keeps advanced controls folded until asked for", async () => {
    const user = userEvent.setup();
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={vi.fn()} />);

    await openPopover(user);
    const toggle = screen.getByRole("button", { name: /Advanced/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("slider", { name: "Top K" })).not.toBeInTheDocument();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("slider", { name: "Top K" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Seed" })).toHaveAttribute("placeholder", "Random");
  });

  it("opens the advanced section by itself when it holds an override", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GenerationParamsPopover value={{ seed: 7 }} defaults={DEFAULTS} onChange={onChange} />);

    await openPopover(user);
    expect(screen.getByRole("button", { name: /Advanced/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Advanced/ })).toHaveTextContent("1 changed");
    const seed = screen.getByRole("textbox", { name: "Seed" });
    expect(seed).toHaveValue("7");

    // An empty seed is the visible "Random" state, not a pinned zero.
    await user.clear(seed);
    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it("reset to defaults calls onChange(null) and only appears when active", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GenerationParamsPopover value={{ temperature: 0.2 }} defaults={DEFAULTS} onChange={onChange} />);

    await openPopover(user);
    await user.click(screen.getByRole("button", { name: "Reset to defaults" }));

    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("does not show reset when there is no active override", async () => {
    const user = userEvent.setup();
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={vi.fn()} />);

    await openPopover(user);

    expect(screen.queryByRole("button", { name: "Reset to defaults" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reset Temperature/ })).not.toBeInTheDocument();
  });

  it("disables the trigger when disabled is set", () => {
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} disabled onChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Generation parameters for this chat" })).toBeDisabled();
  });
});
