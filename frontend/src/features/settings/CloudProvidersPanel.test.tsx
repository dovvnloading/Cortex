import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CloudCatalog } from "../../hooks/useCloudModels";
import { CloudProvidersPanel } from "./CloudProvidersPanel";

const openai = { id: "openai", label: "OpenAI", configured: true, setup: "Set OPENAI_API_KEY." };
const other = { id: "other", label: "Other Lab", configured: true, setup: "Set OTHER_KEY." };
const cloud: CloudCatalog = {
  providers: [openai, other], catalogs: { openai: { models: ["cloud:openai:model"] }, other: { models: [] } },
  models: ["cloud:openai:model"], error: null, refresh: vi.fn(async () => {}),
};

describe("Cloud providers in Settings", () => {
  it("initially inspects the selected chat model’s provider while letting the user inspect another", async () => {
    const user = userEvent.setup();
    render(<CloudProvidersPanel cloud={cloud} selectedProviderId="other" />);
    const select = screen.getByRole("combobox", { name: "Cloud provider" });
    expect(select).toHaveTextContent("Other Lab");
    expect(screen.getByText("Set OTHER_KEY.")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(/no chat models/i);
    await user.click(select);
    await user.click(await screen.findByRole("option", { name: "OpenAI" }));
    expect(screen.getByText("Set OPENAI_API_KEY.")).toBeVisible();
    expect(screen.getByText("1 chat model available")).toBeVisible();
  });

  it("distinguishes first load, empty registry, and first registry failure", () => {
    const { rerender } = render(<CloudProvidersPanel cloud={{ ...cloud, providers: null }} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading cloud providers");
    rerender(<CloudProvidersPanel cloud={{ ...cloud, providers: [] }} />);
    expect(screen.getByRole("status")).toHaveTextContent("No cloud providers");
    rerender(<CloudProvidersPanel cloud={{ ...cloud, providers: [], error: "Cortex is offline." }} />);
    expect(screen.getByRole("status")).toHaveTextContent("Cortex is offline.");
    expect(screen.queryByText(/no cloud providers/i)).not.toBeInTheDocument();
  });

  it("shows partial discovery failures and labels the retained model count as stale", async () => {
    const user = userEvent.setup();
    const catalogs = { ...cloud.catalogs, other: { models: ["cloud:other:chat"], error: "Catalog timed out." } };
    render(<CloudProvidersPanel cloud={{ ...cloud, catalogs }} />);
    await user.click(screen.getByRole("combobox", { name: "Cloud provider" }));
    const option = await screen.findByRole("option", { name: "Other Lab" });
    expect(option).toHaveAccessibleDescription("Refresh failed");
    expect(within(option).getByText("Refresh failed")).toBeVisible();
    await user.click(option);
    expect(screen.getByText("Catalog timed out.")).toBeVisible();
    expect(screen.getByText(/Last successful check found 1 chat model\./)).toBeVisible();
  });
});
