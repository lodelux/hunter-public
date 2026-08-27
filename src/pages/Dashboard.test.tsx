import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import Dashboard from "./Dashboard";
import {
  checkHealth,
  getAutomation,
  getChromiumStatus,
  getDashboardData,
  getSetupStatus,
  getSystemStatus,
  startAutomation,
  stopAutomation,
} from "../lib/api";

const navigateMock = vi.fn();

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { changeLanguage: vi.fn() },
  }),
}));

vi.mock("react-router-dom", async (original) => {
  const actual = await original<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigateMock };
});

vi.mock("../lib/api", () => ({
  getDashboardData: vi.fn(),
  checkHealth: vi.fn(),
  getSetupStatus: vi.fn(),
  getChromiumStatus: vi.fn(),
  getAutomation: vi.fn(),
  getSystemStatus: vi.fn(),
  startAutomation: vi.fn(),
  stopAutomation: vi.fn(),
}));

const mockGetDashboardData = vi.mocked(getDashboardData);
const mockCheckHealth = vi.mocked(checkHealth);
const mockGetSetupStatus = vi.mocked(getSetupStatus);
const mockGetChromiumStatus = vi.mocked(getChromiumStatus);
const mockGetAutomation = vi.mocked(getAutomation);
const mockGetSystemStatus = vi.mocked(getSystemStatus);
const mockStartAutomation = vi.mocked(startAutomation);

const categoryCounts = {
  review: 2,
  qualified: 3,
  unqualified: 1,
  applied: 6,
  online_assessment: 2,
  rejected: 1,
  interview: 1,
  offer: 1,
  accepted: 0,
  refused: 0,
  failed: 1,
  blocked: 1,
};

const dashboardData = {
  jobs: {
    total: 10,
    pending: 169,
    qualified: 1,
    applied: 6,
    failed: 1,
    blocked: 1,
    in_progress: 0,
    category_counts: categoryCounts,
  },
  memory: { total_memories: 42, unique_domains: 5, by_category: {} },
  gmail_outcomes: {
    client_configured: true,
    connected: true,
    enabled: true,
    account_email: "candidate@example.com",
    last_success_at: "2026-08-20T12:00:00Z",
    last_error: null,
    last_sync_summary: { scanned: 1, updated: 0, review_required: 0 },
    review_required_count: 0,
    review_required: [],
    recent_events: [],
    telegram_configured: true,
  },
};

const setupIncomplete = {
  profile: true,
  llm: false,
  resume: false,
  chromium: true,
  linkedin: true,
  gmail: true,
  onboarding_completed: false,
  all_required_done: false,
};

const automation = {
  config: {
    enabled: false,
    sources: ["linkedin", "indeed"] as const,
    interval_minutes: 15 as const,
    daily_job_limit: 10,
    daily_company_limit: 5,
    max_jobs_per_source: 5,
    max_applications_per_cycle: 2,
    automation_min_score: 70,
    linkedin_outreach_enabled: true,
    hours_old: 1,
    title: "",
    location: "",
    job_type: "",
    is_remote: false,
    linkedin_search_url: "",
  },
  status: {
    state: "stopped" as const,
    phase: "idle" as const,
    current_source: null,
    current_job_url: null,
    next_run_at: null,
    last_cycle_started_at: null,
    last_cycle_finished_at: null,
    today_started: 0,
    today_limit: 10,
    queued_count: 0,
    captcha_count: 0,
    pause_reason: null,
    log: [],
  },
};

const machineStatus = {
  collected_at: "2026-08-21T12:00:00Z",
  cpu: { percent: 24 },
  memory: { percent: 52, used_bytes: 2_000_000_000, total_bytes: 4_000_000_000, available_bytes: 1_920_000_000 },
  swap: { percent: 10, used_bytes: 400_000_000, total_bytes: 4_000_000_000 },
  disk: {
    percent: 20,
    used_bytes: 80_000_000_000,
    total_bytes: 400_000_000_000,
    free_bytes: 320_000_000_000,
    hunter_data_bytes: 4_000_000_000,
  },
  temperature: {
    available: true,
    celsius: 58,
    sensor: "CPU package",
    high_celsius: 80,
    critical_celsius: 95,
  },
  headroom: {
    state: "healthy" as const,
    summary: "Resources are within a safe range for browser work.",
  },
};

function renderDashboard() {
  return render(
    <MemoryRouter>
      <Dashboard />
    </MemoryRouter>,
  );
}

describe("Dashboard page", () => {
  beforeEach(() => {
    navigateMock.mockReset();
    mockCheckHealth.mockResolvedValue({} as never);
    mockGetDashboardData.mockResolvedValue(structuredClone(dashboardData) as never);
    mockGetSetupStatus.mockResolvedValue(structuredClone(setupIncomplete) as never);
    mockGetChromiumStatus.mockResolvedValue({ state: "ready", message: "" } as never);
    mockGetAutomation.mockResolvedValue(structuredClone(automation) as never);
    mockGetSystemStatus.mockResolvedValue(structuredClone(machineStatus) as never);
    mockStartAutomation.mockResolvedValue({ success: true, ...structuredClone(automation) } as never);
    vi.mocked(stopAutomation).mockResolvedValue({ success: true, ...structuredClone(automation) } as never);
  });

  it("renders the new workspace header while dashboard data is loading", () => {
    mockGetDashboardData.mockReturnValue(new Promise(() => {}) as never);
    renderDashboard();
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.queryByText("Search pipeline")).not.toBeInTheDocument();
  });

  it("renders the runner control room and search pipeline", async () => {
    renderDashboard();
    expect(await screen.findByText("Runner is ready when you are")).toBeInTheDocument();
    expect(await screen.findByText("Worker headroom")).toBeInTheDocument();
    expect(screen.getByText("Resources are within a safe range for browser work.")).toBeInTheDocument();
    expect(screen.getByText("58°C")).toBeInTheDocument();
    expect(screen.getByText("373 GB total")).toBeInTheDocument();
    expect(screen.getByText("Hunter 3.7 GB")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Hunter data uses 3.7 GB/ })).toBeInTheDocument();
    expect(screen.getByText("Search pipeline")).toBeInTheDocument();
    expect(screen.getByText("Discovered")).toBeInTheDocument();
    expect(screen.getByText("Qualified")).toBeInTheDocument();
    expect(screen.getByText("Pending").previousElementSibling).toHaveTextContent("1");
    expect(screen.getByText("86% verified attempt success")).toBeInTheDocument();
  });

  it("starts the autonomous runner from the primary control", async () => {
    const user = userEvent.setup();
    renderDashboard();
    await user.click(await screen.findByRole("button", { name: "Start runner" }));
    await waitFor(() => expect(mockStartAutomation).toHaveBeenCalled());
  });

  it("navigates to runner configuration and the selected attention queue", async () => {
    const user = userEvent.setup();
    renderDashboard();
    await screen.findByText("Runner is ready when you are");

    await user.click(screen.getByRole("button", { name: "Configure runner" }));
    expect(navigateMock).toHaveBeenCalledWith("/jobs");

    await user.click(screen.getByRole("button", { name: /Unreviewed jobs/ }));
    expect(navigateMock).toHaveBeenCalledWith("/jobs?tab=pending&category=review");
  });

  it("routes Gmail outcome attention to the independent email review panel", async () => {
    mockGetDashboardData.mockResolvedValue({
      ...structuredClone(dashboardData),
      gmail_outcomes: {
        ...structuredClone(dashboardData.gmail_outcomes),
        review_required_count: 1,
      },
    } as never);
    const user = userEvent.setup();
    renderDashboard();

    await user.click(await screen.findByRole("button", { name: /Email outcomes/ }));

    expect(navigateMock).toHaveBeenCalledWith("/jobs?tab=pending&category=rejected&review=email");
  });

  it("shows unfinished setup work and routes directly to the missing item", async () => {
    const user = userEvent.setup();
    renderDashboard();
    expect(await screen.findByText("setup.gettingStarted")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "setup.configureAI" }));
    expect(navigateMock).toHaveBeenCalledWith("/llm");
  });

  it("removes setup clutter once required setup is complete", async () => {
    mockGetSetupStatus.mockResolvedValue({
      ...setupIncomplete,
      llm: true,
      resume: true,
      all_required_done: true,
    } as never);
    renderDashboard();
    expect(await screen.findByText("Runner is ready when you are")).toBeInTheDocument();
    expect(screen.queryByText("setup.gettingStarted")).not.toBeInTheDocument();
  });

  it("does not show the backend error while health is OK", async () => {
    renderDashboard();
    await screen.findByText("Runner is ready when you are");
    expect(screen.queryByText(/backend is not connected/i)).not.toBeInTheDocument();
  });

  it("shows Chromium installation progress", async () => {
    mockGetChromiumStatus.mockResolvedValue({ state: "installing", message: "Downloading 45%" } as never);
    renderDashboard();
    expect(await screen.findByText("Downloading 45%")).toBeInTheDocument();
    expect(screen.getByText("chromium.installing")).toBeInTheDocument();
  });

  it("still renders the workspace when dashboard metrics fail", async () => {
    mockGetDashboardData.mockRejectedValue(new Error("500"));
    renderDashboard();
    expect(await screen.findByText("Runner is ready when you are")).toBeInTheDocument();
    expect(screen.getByText("Search pipeline")).toBeInTheDocument();
  });

  it("leaves the loading screen after all startup health checks fail", async () => {
    vi.useFakeTimers();
    mockCheckHealth.mockRejectedValue(new Error("offline"));
    try {
      renderDashboard();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(13500);
      });
      expect(screen.getByText("Runner is ready when you are")).toBeInTheDocument();
      expect(screen.getByText(/backend is not connected/i)).toBeInTheDocument();
      expect(mockGetDashboardData).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("re-fetches setup status when setup changes", async () => {
    mockGetSetupStatus.mockResolvedValue({ ...setupIncomplete, profile: false } as never);
    renderDashboard();
    await screen.findByText("Runner is ready when you are");
    const initialCalls = mockGetSetupStatus.mock.calls.length;

    window.dispatchEvent(new CustomEvent("langhire:setup-updated"));
    await waitFor(() => expect(mockGetSetupStatus.mock.calls.length).toBeGreaterThan(initialCalls));
  });
});
