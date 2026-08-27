import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getApplicationAttempt } from "../../lib/api";
import type { ApplicationAttemptDossier } from "../../lib/types";
import DossierViewer from "./DossierViewer";

vi.mock("../../lib/api", () => ({
  getApplicationAttempt: vi.fn(),
  getApplicationAttemptFile: vi.fn(),
}));

const mockGetApplicationAttempt = vi.mocked(getApplicationAttempt);

function dossier(): ApplicationAttemptDossier {
  return {
    manifest: {
      attempt_id: "attempt-1",
      status: "applied",
      job_url: "https://example.com/job",
      job_title: "Backend Engineer",
      company: "Acme",
      started_at: "2026-08-25T10:00:00Z",
      finished_at: "2026-08-25T10:05:00Z",
      duration_seconds: 300,
      step_count: 4,
      agent_claimed_success: true,
      judge_verdict: true,
      submission_confirmed: true,
      judgement: null,
      confirmation_evidence: null,
      error: null,
      errors: [],
      recording_error: null,
      cost_usd: 0.2,
      cost_breakdown: null,
      token_breakdown: {
        cv: {
          prompt_tokens: 8_000,
          cached_prompt_tokens: 6_000,
          cache_write_tokens: 0,
          completion_tokens: 1_200,
          visible_completion_tokens: 900,
          reasoning_tokens: 300,
          total_tokens: 9_200,
          requests: 1,
          models: {},
        },
      },
    },
    steps: [],
    screenshots: [],
    recording: null,
    files: [],
    original_listing_text: "Build reliable APIs and maintain the data platform.",
    generation_audit: {
      resume: {
        model: "gpt-5.6-sol",
        reasoning_effort: "medium",
        positioning: "Lead with reliable backend delivery.",
        headline: {
          text: "Backend Engineer",
          requirements: [{ id: "J001", text: "Backend Engineer" }],
        },
        priorities: [{
          decision: "Emphasize production API ownership.",
          requirements: [{ id: "J002", text: "Build reliable APIs" }],
          evidence: [{ id: "P014", text: "Owned Python APIs in production" }],
        }],
        selections: [{
          section: "Experience",
          label: "Engineer at Example",
          text: "Delivered reliable Python APIs.",
          relevance: 96,
          requirements: [{ id: "J002", text: "Build reliable APIs" }],
          evidence: [{ id: "P014", text: "Owned Python APIs in production" }],
        }],
      },
      cover_letter: {
        model: "gpt-5.6-sol",
        reasoning_effort: "medium",
        positioning: "Connect hands-on delivery to Acme's immediate need.",
        decisions: [{
          decision: "Open with the strongest delivery match.",
          requirements: [{ id: "J002", text: "Build reliable APIs" }],
          evidence: [{ id: "P014", text: "Owned Python APIs in production" }],
        }],
      },
    },
  };
}

describe("DossierViewer document audit", () => {
  beforeEach(() => {
    mockGetApplicationAttempt.mockResolvedValue(dossier());
  });

  it("connects document decisions to target needs and candidate evidence", async () => {
    render(<DossierViewer attemptId="attempt-1" onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText("Why these documents look this way")).toBeInTheDocument();
    });
    expect(screen.getByText("Why these documents look this way").closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("Lead with reliable backend delivery.")).toBeInTheDocument();
    expect(screen.getByText("Connect hands-on delivery to Acme's immediate need.")).toBeInTheDocument();
    expect(screen.getAllByText("Build reliable APIs").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Owned Python APIs in production").length).toBeGreaterThan(0);
    expect(screen.getByText(/8,000 prompt.*900 visible output.*300 reasoning tokens/)).toBeInTheDocument();
    expect(screen.getByText("Inspect 1 selected CV claims")).toBeInTheDocument();
  });

  it("offers the archived listing text when the external listing is unavailable", async () => {
    render(<DossierViewer attemptId="attempt-1" onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText("View original listing text")).toBeInTheDocument();
    });
    const listingDisclosure = screen.getByText("View original listing text").closest("details");
    expect(listingDisclosure).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("View original listing text"));
    expect(listingDisclosure).toHaveAttribute("open");
    expect(screen.getByText("Build reliable APIs and maintain the data platform.")).toBeInTheDocument();
  });
});
