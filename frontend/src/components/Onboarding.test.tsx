import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Onboarding } from "./Onboarding";

describe("Onboarding", () => {
  it("does not expose a manual sign-in control without a desktop handoff", () => {
    const onSubmit = vi.fn<(token: string) => Promise<void>>().mockResolvedValue();
    render(<Onboarding initialToken="" error={null} busy={false} onSubmit={onSubmit} />);

    expect(screen.getByRole("heading", { name: "Start local workspace" })).toBeVisible();
    expect(screen.queryByLabelText(/token/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("uses a launcher-provided token automatically for the owned desktop window", async () => {
    const onSubmit = vi.fn<(token: string) => Promise<void>>().mockResolvedValue();
    const { container } = render(<Onboarding initialToken="desktop-handoff" error={null} busy={false} onSubmit={onSubmit} />);

    expect(screen.getByRole("heading", { name: "Opening local workspace" })).toBeVisible();
    expect(container.querySelector("#bootstrap-token")).not.toBeInTheDocument();
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith("desktop-handoff"));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("offers a retry without asking for the handoff value again", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn<(token: string) => Promise<void>>().mockResolvedValue();
    render(<Onboarding initialToken="desktop-handoff" error="Workspace startup failed." busy={false} onSubmit={onSubmit} />);

    expect(screen.getByRole("alert")).toHaveTextContent("Workspace startup failed.");
    const retry = screen.getByRole("button", { name: "Retry workspace startup" });
    await user.click(retry);
    expect(onSubmit).toHaveBeenCalledWith("desktop-handoff");
  });
});
