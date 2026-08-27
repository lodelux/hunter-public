import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { dismissGmailOutcomeReview, openTrustedExternalUrl } from "../../lib/api";
import type { GmailOutcomeStatus } from "../../lib/types";
import GmailOutcomeReviewPanel from "./GmailOutcomeReviewPanel";

vi.mock("../../lib/api", () => ({
  dismissGmailOutcomeReview: vi.fn(),
  openTrustedExternalUrl: vi.fn(),
}));

const mockDismissGmailOutcomeReview = vi.mocked(dismissGmailOutcomeReview);
const mockOpenTrustedExternalUrl = vi.mocked(openTrustedExternalUrl);

const status: GmailOutcomeStatus = {
  client_configured: true,
  connected: true,
  enabled: true,
  account_email: "candidate@example.com",
  last_success_at: "2026-08-24T12:00:00Z",
  last_error: null,
  last_sync_summary: { scanned: 1, updated: 0, review_required: 1 },
  review_required_count: 1,
  review_required: [{
    message_id: "message-1",
    thread_id: "thread-1",
    received_at: "2026-08-24T11:30:00Z",
    subject: "Update on your application",
    sender: "Acme Recruiting",
    reason: "The message describes a next step but does not explicitly confirm an interview.",
    gmail_url: "https://mail.google.com/mail/u/?authuser=candidate%40example.com#all/thread-1",
    job_url: "https://jobs.example/acme-engineer",
    company: "Acme",
    title: "Software Engineer",
  }],
  recent_events: [],
  telegram_configured: true,
};

function renderPanel(overrides: Partial<React.ComponentProps<typeof GmailOutcomeReviewPanel>> = {}) {
  const onRefresh = vi.fn(async () => {});
  render(
    <MemoryRouter>
      <GmailOutcomeReviewPanel
        status={status}
        loading={false}
        loadError={null}
        onRefresh={onRefresh}
        {...overrides}
      />
    </MemoryRouter>,
  );
  return { onRefresh };
}

describe("GmailOutcomeReviewPanel", () => {
  beforeEach(() => {
    mockDismissGmailOutcomeReview.mockResolvedValue({ success: true });
    mockOpenTrustedExternalUrl.mockResolvedValue(undefined);
  });

  it("shows the matched job context and opens the source email", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByRole("button", { name: "Show" }));

    expect(screen.getByText("Acme · Software Engineer")).toBeInTheDocument();
    expect(screen.getByText("Matched job")).toBeInTheDocument();
    expect(screen.getByText(/does not explicitly confirm an interview/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Open Gmail" }));
    expect(mockOpenTrustedExternalUrl).toHaveBeenCalledWith(status.review_required[0].gmail_url);
  });

  it("marks an item reviewed and explains that the job outcome is unchanged", async () => {
    const user = userEvent.setup();
    const { onRefresh } = renderPanel();
    await user.click(screen.getByRole("button", { name: "Show" }));

    expect(screen.getByText(/does not change the job’s hiring outcome/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Mark reviewed" }));

    await waitFor(() => expect(mockDismissGmailOutcomeReview).toHaveBeenCalledWith("message-1"));
    expect(onRefresh).toHaveBeenCalled();
  });

  it("starts collapsed with a notification count and expands on request", async () => {
    const user = userEvent.setup();
    renderPanel();

    const showButton = screen.getByRole("button", { name: "Show" });
    expect(showButton).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByLabelText("1 email review waiting")).toHaveTextContent("1");
    expect(screen.queryByText("Update on your application")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Refresh" })).not.toBeInTheDocument();

    await user.click(showButton);
    expect(screen.getByRole("button", { name: "Hide" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Update on your application")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
  });

  it("keeps the review destination useful when the queue is empty", async () => {
    const user = userEvent.setup();
    renderPanel({
      status: { ...status, review_required_count: 0, review_required: [] },
    });
    await user.click(screen.getByRole("button", { name: "Show" }));

    expect(screen.getByText("No email outcomes need review")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Gmail settings" })).toHaveAttribute("href", "/settings?section=gmail");
  });
});
