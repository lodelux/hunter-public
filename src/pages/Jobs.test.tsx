import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import Jobs from "./Jobs";
import { getGmailOutcomeStatus, getJobStats } from "../lib/api";

vi.mock("./jobs/CollectTab", () => ({
  default: ({ onJobsChanged }: { onJobsChanged: () => void }) => (
    <div data-testid="collect-tab"><button onClick={onJobsChanged}>collect-refresh</button></div>
  ),
}));
vi.mock("./jobs/ReviewApplyTab", () => ({
  default: ({ category }: { category: string }) => (
    <div data-testid="review-apply-tab" data-category={category} />
  ),
}));
vi.mock("../components/jobs/GmailOutcomeReviewPanel", () => ({
  default: ({ status }: { status: { review_required_count: number } | null }) => (
    <div data-testid="gmail-outcome-review-panel">{status?.review_required_count || 0} email reviews</div>
  ),
}));
vi.mock("../lib/api", () => ({
  getJobStats: vi.fn(),
  getGmailOutcomeStatus: vi.fn(),
}));

const mockGetJobStats = vi.mocked(getJobStats);
const mockGetGmailOutcomeStatus = vi.mocked(getGmailOutcomeStatus);
const stats = {
  total: 10,
  pending: 4,
  applied: 6,
  failed: 2,
  blocked: 1,
  in_progress: 0,
  category_counts: {
    review: 4,
    qualified: 10,
    unqualified: 5,
    applied: 3,
    online_assessment: 2,
    rejected: 2,
    interview: 1,
    offer: 1,
    accepted: 1,
    refused: 1,
    failed: 2,
    blocked: 1,
  },
};

const gmailStatus = {
  client_configured: true,
  connected: true,
  enabled: true,
  account_email: "candidate@example.com",
  last_success_at: "2026-08-24T12:00:00Z",
  last_error: null,
  last_sync_summary: { scanned: 1, updated: 0, review_required: 2 },
  review_required_count: 2,
  review_required: [],
  recent_events: [],
  telegram_configured: true,
};

function renderJobs(initialEntry = "/jobs") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Jobs />
    </MemoryRouter>,
  );
}

describe("Jobs", () => {
  beforeEach(() => {
    mockGetJobStats.mockResolvedValue(structuredClone(stats) as never);
    mockGetGmailOutcomeStatus.mockResolvedValue(structuredClone(gmailStatus) as never);
  });

  it("fetches stats and renders the runner-first header", async () => {
    renderJobs();
    expect(screen.getByText("Runner & jobs")).toBeInTheDocument();
    await waitFor(() => expect(mockGetJobStats).toHaveBeenCalled());
  });

  it("opens the autonomous runner by default", async () => {
    renderJobs();
    expect(await screen.findByTestId("collect-tab")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Autonomous runner/ })).toHaveAttribute("aria-selected", "true");
  });

  it("switches to the review workspace", async () => {
    const user = userEvent.setup();
    renderJobs();
    await user.click(screen.getByRole("tab", { name: /Review jobs/ }));
    expect(await screen.findByTestId("review-apply-tab")).toHaveAttribute("data-category", "review");
    const emailPanel = screen.getByTestId("gmail-outcome-review-panel");
    const jobsList = screen.getByTestId("review-apply-tab");
    expect(emailPanel).toHaveTextContent("2 email reviews");
    expect(emailPanel.compareDocumentPosition(jobsList) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByTestId("collect-tab")).not.toBeInTheDocument();
  });

  it("keeps excluded jobs outside the three active stages", async () => {
    renderJobs("/jobs?tab=pending");
    await waitFor(() => expect(mockGetJobStats).toHaveBeenCalled());

    const stageButtons = ["Review queue", "Applications", "Outcomes"].map((name) =>
      screen.getByRole("button", { name: new RegExp(name) }),
    );
    expect(stageButtons.map((button) => button.textContent)).toEqual([
      expect.stringContaining("Review queue"),
      expect.stringContaining("Applications"),
      expect.stringContaining("Outcomes"),
    ]);
    expect(within(screen.getByRole("tablist", { name: "Job folders" })).getAllByRole("tab")).toHaveLength(2);
    expect(screen.getByRole("button", { name: /Excluded/ })).toHaveTextContent("6");
  });

  it("moves from the review queue into application folders", async () => {
    const user = userEvent.setup();
    renderJobs("/jobs?tab=pending");
    await user.click(await screen.findByRole("button", { name: /Applications/ }));
    await user.click(screen.getByRole("tab", { name: "Applied (3)" }));
    expect(screen.getByTestId("review-apply-tab")).toHaveAttribute("data-category", "applied");
  });

  it("opens Ineligible from the secondary Excluded rail", async () => {
    const user = userEvent.setup();
    renderJobs("/jobs?tab=pending");
    await user.click(await screen.findByRole("button", { name: /Excluded/ }));
    await user.click(screen.getByRole("tab", { name: "Ineligible (5)" }));
    expect(screen.getByTestId("review-apply-tab")).toHaveAttribute("data-category", "unqualified");
  });

  it("deep-links to a category and opens its stage", async () => {
    renderJobs("/jobs?tab=pending&category=offer");
    expect(await screen.findByRole("tab", { name: "Offer (1)" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("review-apply-tab")).toHaveAttribute("data-category", "offer");
  });

  it("keeps email reviews independent from the selected job folder", async () => {
    const user = userEvent.setup();
    renderJobs("/jobs?tab=pending&category=rejected&review=email");

    expect(await screen.findByRole("tab", { name: "Rejected (2)" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("gmail-outcome-review-panel")).toHaveTextContent("2 email reviews");
    expect(screen.getByTestId("review-apply-tab")).toHaveAttribute("data-category", "rejected");
    expect(screen.getByRole("button", { name: /Outcomes/ })).toHaveTextContent("8");
    expect(screen.getByRole("button", { name: /Outcomes/ })).toHaveTextContent("2 to review");

    await user.click(screen.getByRole("button", { name: /Applications/ }));
    expect(screen.getByTestId("review-apply-tab")).toHaveAttribute("data-category", "applied");
    expect(screen.getByTestId("gmail-outcome-review-panel")).toHaveTextContent("2 email reviews");
    expect(screen.queryByRole("tab", { name: /Email review/ })).not.toBeInTheDocument();
  });

  it("re-fetches stats when a child reports changes", async () => {
    const user = userEvent.setup();
    renderJobs();
    await waitFor(() => expect(mockGetJobStats).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "collect-refresh" }));
    await waitFor(() => expect(mockGetJobStats).toHaveBeenCalledTimes(2));
  });

  it("renders stage totals derived from folder counts", async () => {
    renderJobs("/jobs?tab=pending");
    await waitFor(() => expect(mockGetJobStats).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: /Review queue/ })).toHaveTextContent("14");
    expect(screen.getByRole("button", { name: /Applications/ })).toHaveTextContent("5");
    expect(screen.getByRole("button", { name: /Outcomes/ })).toHaveTextContent("8");
  });

  it("does not crash when stats are unavailable", async () => {
    mockGetJobStats.mockRejectedValue(new Error("down"));
    renderJobs();
    expect(await screen.findByTestId("collect-tab")).toBeInTheDocument();
  });

  it("keeps legacy history links in the review workspace", async () => {
    renderJobs("/jobs?tab=history");
    expect(await screen.findByTestId("review-apply-tab")).toHaveAttribute("data-category", "review");
  });
});
