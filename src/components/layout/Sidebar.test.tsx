import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Sidebar from "./Sidebar";
import { checkHealth } from "../../lib/api";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { changeLanguage: vi.fn(), dir: () => "ltr", language: "en" },
  }),
}));
vi.mock("../../lib/api", () => ({ checkHealth: vi.fn() }));

const mockCheckHealth = vi.mocked(checkHealth);

function renderSidebar(initialEntries: string[] = ["/"]) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <Sidebar />
    </MemoryRouter>,
  );
}

describe("Sidebar", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockCheckHealth.mockResolvedValue({ build_id: "abc123" } as never);
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it("renders a grouped runner-first navigation", () => {
    renderSidebar();
    expect(screen.getAllByText("app.title")).toHaveLength(2);
    expect(screen.getByText("Workspace")).toBeInTheDocument();
    expect(screen.getByText("Application kit")).toBeInTheDocument();
    expect(screen.getByText("Review")).toBeInTheDocument();
    expect(screen.getByText("System")).toBeInTheDocument();

    for (const key of [
      "nav.overview",
      "nav.runner",
      "nav.profile",
      "nav.qa",
      "nav.memory",
      "nav.analytics",
      "nav.logs",
      "nav.settings",
    ]) {
      expect(screen.getByText(key)).toBeInTheDocument();
    }
  });

  it("keeps the primary destinations available on desktop and mobile", () => {
    renderSidebar();
    expect(screen.getAllByRole("link", { name: "nav.overview" })).toHaveLength(2);
    expect(screen.getAllByRole("link", { name: "nav.runner" })).toHaveLength(2);
    expect(screen.getAllByRole("link", { name: "nav.settings" })).toHaveLength(2);
    expect(screen.getAllByRole("link", { name: "nav.runner" })[0]).toHaveAttribute("href", "/jobs");
  });

  it("marks the active route without turning the whole sidebar into equal pills", () => {
    renderSidebar(["/jobs"]);
    const runnerLinks = screen.getAllByRole("link", { name: "nav.runner" });
    expect(runnerLinks.every((link) => link.getAttribute("aria-current") === "page")).toBe(true);
    expect(runnerLinks[0]).toHaveClass("bg-primary/10");
    expect(screen.getAllByRole("link", { name: "nav.overview" })[0]).not.toHaveAttribute("aria-current", "page");
  });

  it("shows connecting state then the connected build", async () => {
    renderSidebar();
    expect(screen.getByText("status.connecting")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/status\.connected/)).toBeInTheDocument());
    expect(screen.getByText(/abc123/)).toBeInTheDocument();
  });

  it("shows backend startup state when health fails", async () => {
    mockCheckHealth.mockRejectedValue(new Error("backend down"));
    renderSidebar();
    await waitFor(() => expect(screen.getByText("status.startingBackend")).toBeInTheDocument());
  });

  it("renders connected without a build id when none is returned", async () => {
    mockCheckHealth.mockResolvedValue({ build_id: "" } as never);
    renderSidebar();
    await waitFor(() => expect(screen.getByText("status.connected")).toBeInTheDocument());
    expect(screen.queryByText(/·/)).not.toBeInTheDocument();
  });
});
