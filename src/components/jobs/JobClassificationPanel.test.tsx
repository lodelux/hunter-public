import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Job } from "../../lib/types";
import { updateScreeningOverride } from "../../lib/api";
import JobClassificationPanel from "./JobClassificationPanel";

vi.mock("../../lib/api", () => ({
  updateScreeningOverride: vi.fn(),
}));

const baseJob: Job = {
  url: "https://jobs.example/engineer",
  title: "Backend Engineer",
  company: "Acme",
  location: "Berlin",
  easy_apply: true,
  status: "pending",
  category: "review",
};

const classifiedJob: Job = {
  ...baseJob,
  classification: {
    status: "complete",
    model: "gpt-5.4-mini",
    schema_version: 1,
    classified_at: "2026-07-27T20:01:02Z",
    description_hash: "description",
    profile_hash: "profile",
    score: 72,
    error: null,
    facts: {
      seniority: {
        value: "mid",
        confidence: 0.91,
        evidence: ["Mid-level backend engineer"],
      },
      experience_years: {
        value: 3,
        confidence: 0.88,
        evidence: ["3+ years of experience"],
      },
      work_mode: {
        value: "hybrid",
        confidence: 0.96,
        evidence: ["Two office days per week"],
      },
      required_languages: [
        { value: "English", confidence: 0.99, evidence: ["Fluent English required"] },
      ],
      preferred_languages: [],
      required_skills: [
        { value: "Python", confidence: 0.98, evidence: ["Strong Python skills"] },
      ],
      preferred_skills: [],
      education: {
        value: null,
        confidence: 0.1,
        evidence: ["No degree requirement stated"],
      },
      salary: {
        minimum: 60_000,
        maximum: 75_000,
        currency: "EUR",
        interval: "annual",
        confidence: 0.94,
        evidence: ["€60,000–€75,000"],
      },
      sponsorship: {
        value: "unavailable",
        confidence: 0.9,
        evidence: ["Unable to sponsor visas"],
      },
    },
    assessment: {
      role: { rating: "excellent", reason: "Direct match for the target role." },
      skills: { rating: "good", reason: "Most required skills are present." },
      experience: { rating: "partial", reason: "Slightly below the preferred tenure." },
      location_work_mode: { rating: "good", reason: "Hybrid work is acceptable." },
      language_eligibility: { rating: "excellent", reason: "English requirement is met." },
      salary: { rating: "good", reason: "Compensation meets expectations." },
    },
  },
  screening: {
    status: "rejected",
    reasons: [
      {
        code: "sponsorship_unavailable",
        message: "The listing explicitly says sponsorship is unavailable",
        confidence: 0.9,
      },
    ],
    policy_version: 1,
    evaluated_at: "2026-07-27T20:01:03Z",
    override: null,
  },
};

describe("JobClassificationPanel", () => {
  it("shows the score and screening status, then expands assessment and facts", async () => {
    const user = userEvent.setup();
    render(<JobClassificationPanel job={classifiedJob} />);

    expect(screen.getByText("72/100")).toBeInTheDocument();
    expect(screen.getByText("Rejected")).toBeInTheDocument();
    expect(screen.queryByText("Fit assessment")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /AI analysis/ }));

    expect(screen.getByText("Fit assessment")).toBeInTheDocument();
    expect(screen.getByText("Direct match for the target role.")).toBeInTheDocument();
    expect(screen.getByText("Python")).toBeInTheDocument();
    expect(screen.getByText("“Strong Python skills”")).toBeInTheDocument();
    expect(screen.getByText("EUR 60,000–75,000 / annual")).toBeInTheDocument();
    expect(
      screen.getByText("The listing explicitly says sponsorship is unavailable (90% confident)"),
    ).toBeInTheDocument();
    expect(screen.getByText(/gpt-5\.4-mini · schema v1/)).toBeInTheDocument();
  });

  it("allows a rejected job to be overridden as qualified", async () => {
    const user = userEvent.setup();
    const onScreeningChanged = vi.fn();
    vi.mocked(updateScreeningOverride).mockResolvedValue({
      success: true,
      status: "qualified",
      override: "qualified",
    });
    render(
      <JobClassificationPanel
        job={classifiedJob}
        onScreeningChanged={onScreeningChanged}
      />,
    );

    await user.click(screen.getByRole("button", { name: /AI analysis/ }));
    await user.click(screen.getByRole("button", { name: "Override rejection" }));

    await waitFor(() => {
      expect(updateScreeningOverride).toHaveBeenCalledWith(
        classifiedJob.url,
        "qualified",
      );
      expect(onScreeningChanged).toHaveBeenCalledOnce();
    });
  });

  it("shows active classification without an expandable empty panel", () => {
    render(
      <JobClassificationPanel
        job={{
          ...baseJob,
          classification: {
            ...classifiedJob.classification!,
            status: "running",
            facts: {},
            assessment: {},
            score: null,
          },
        }}
      />,
    );

    expect(screen.getByText("Classifying with gpt-5.4-mini…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /AI analysis/ })).not.toBeInTheDocument();
  });

  it("surfaces a stored classification error", () => {
    render(
      <JobClassificationPanel
        job={{
          ...baseJob,
          classification: {
            ...classifiedJob.classification!,
            status: "failed",
            facts: {},
            assessment: {},
            score: null,
            error: "API quota exceeded",
          },
        }}
      />,
    );

    expect(
      screen.getByText("Classification failed: API quota exceeded"),
    ).toBeInTheDocument();
  });
});
