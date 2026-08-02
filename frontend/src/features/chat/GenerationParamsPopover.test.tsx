import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { GenerationParamsPopover } from "./GenerationParamsPopover";

const DEFAULTS = { temperature: 0.7, top_p: 0.9, top_k: 40, repeat_penalty: 1.1, num_ctx: 4096, seed: -1 };

describe("GenerationParamsPopover", () => {
  it("shows the trigger without an active indicator when there is no override", () => {
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: "Generation parameters for this chat" });
    expect(trigger.className).not.toContain("icon-button-active");
  });

  it("marks the trigger active and offers reset once an override is set", () => {
    render(<GenerationParamsPopover value={{ temperature: 0.2 }} defaults={DEFAULTS} onChange={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: "Generation parameters for this chat" });
    expect(trigger.className).toContain("icon-button-active");
  });

  it("opens on trigger click and shows default values pre-filled", async () => {
    const user = userEvent.setup();
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Generation parameters for this chat" }));

    expect(await screen.findByText("Parameters for this chat")).toBeInTheDocument();
    const temperatureSlider = screen.getByLabelText(/Temperature/) as HTMLInputElement;
    expect(temperatureSlider.value).toBe("0.7");
  });

  it("calls onChange with an override object when a slider moves", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Generation parameters for this chat" }));
    const temperatureSlider = await screen.findByLabelText(/Temperature/);
    // userEvent.type doesn't apply to range inputs; fireEvent.change goes
    // through React's value-setter tracking correctly, unlike a raw dispatch.
    fireEvent.change(temperatureSlider, { target: { value: "0.2" } });

    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ temperature: 0.2 }));
  });

  it("reset to defaults calls onChange(null) and only appears when active", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GenerationParamsPopover value={{ temperature: 0.2 }} defaults={DEFAULTS} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Generation parameters for this chat" }));
    const reset = await screen.findByRole("button", { name: /Reset to defaults/ });
    await user.click(reset);

    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("does not show reset when there is no active override", async () => {
    const user = userEvent.setup();
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} onChange={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Generation parameters for this chat" }));
    await screen.findByText("Parameters for this chat");

    expect(screen.queryByRole("button", { name: /Reset to defaults/ })).not.toBeInTheDocument();
  });

  it("disables the trigger when disabled is set", () => {
    render(<GenerationParamsPopover value={null} defaults={DEFAULTS} disabled onChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Generation parameters for this chat" })).toBeDisabled();
  });
});
