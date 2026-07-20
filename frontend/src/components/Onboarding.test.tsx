import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Onboarding } from "./Onboarding";

describe("Onboarding", () => {
  it("exposes a labelled launcher-token flow", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn<(token: string) => Promise<void>>().mockResolvedValue();
    render(<Onboarding initialToken="" error={null} busy={false} onSubmit={onSubmit} />);

    expect(screen.getByRole("heading", { name: "Connect to Cortex" })).toBeVisible();
    const input = screen.getByLabelText("Launcher token");
    await user.type(input, " bootstrap-token ");
    await user.click(screen.getByRole("button", { name: "Open workspace" }));

    expect(onSubmit).toHaveBeenCalledWith("bootstrap-token");
  });

  it("uses a launcher-provided token automatically for the owned desktop window", async () => {
    const onSubmit = vi.fn<(token: string) => Promise<void>>().mockResolvedValue();
    const { container } = render(<Onboarding initialToken="desktop-handoff" error={null} busy={false} onSubmit={onSubmit} />);

    expect(screen.getByRole("heading", { name: "Opening Cortex" })).toBeVisible();
    expect(container.querySelector("#bootstrap-token")).not.toBeInTheDocument();
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith("desktop-handoff"));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});
