import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ReviewApplyTab from "./ReviewApplyTab";
import {
  exportJobsCsv,
  getApplicationAttempt,
  getApplicationAttemptFile,
  getApplyStatus,
  getAutomation,
  getJobs,
  getMetricRuns,
  openCoverLetterFile,
  openTailoredResumePdf,
  resolveApplicationOutcome,
  startApplying,
  stopApplying,
  tailorResumes,
  updateJobCategories,
  updateJobCategory,
} from "../../lib/api";
import type { Job, RunMetric } from "../../lib/types";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("../../components/jobs/JobClassificationPanel", () => ({
  default: ({ job }: { job: Job }) => <div data-testid={`classification-${job.url}`} />,
}));

vi.mock("../../lib/api", () => ({
  deleteJobs: vi.fn(),
  exportJobsCsv: vi.fn(),
  getApplicationAttempt: vi.fn(),
  getApplicationAttemptFile: vi.fn(),
  getApplyStatus: vi.fn(),
  getAutomation: vi.fn(),
  getJobs: vi.fn(),
  getMetricRuns: vi.fn(),
  openCoverLetterFile: vi.fn(),
  openTailoredResumePdf: vi.fn(),
  resolveApplicationOutcome: vi.fn(),
  startApplying: vi.fn(),
  stopApplying: vi.fn(),
  tailorResumes: vi.fn(),
  updateJobCategories: vi.fn(),
  updateJobCategory: vi.fn(),
}));

const mockGetJobs = vi.mocked(getJobs);
const mockGetMetrics = vi.mocked(getMetricRuns);
const mockGetAttempt = vi.mocked(getApplicationAttempt);
const mockGetAttemptFile = vi.mocked(getApplicationAttemptFile);
const mockGetApplyStatus = vi.mocked(getApplyStatus);
const mockGetAutomation = vi.mocked(getAutomation);
const mockUpdateCategory = vi.mocked(updateJobCategory);
const mockUpdateCategories = vi.mocked(updateJobCategories);
const mockStartApplying = vi.mocked(startApplying);
const mockStopApplying = vi.mocked(stopApplying);
const mockOpenCoverLetter = vi.mocked(openCoverLetterFile);
const mockOpenResume = vi.mocked(openTailoredResumePdf);
const mockResolveApplicationOutcome = vi.mocked(resolveApplicationOutcome);
const mockTailorResumes = vi.mocked(tailorResumes);
const mockExport = vi.mocked(exportJobsCsv);

const baseJob: Job = {
  url: "https://jobs.example/engineer",
  title: "Backend Engineer",
  company: "Acme",
  location: "Berlin",
  easy_apply: true,
  status: "pending",
  category: "qualified",
  collected_at: "2026-08-01T09:00:00Z",
};

const metric: RunMetric = {
  id: 1,
  run_id: "attempt-newest",
  job_url: baseJob.url,
  job_title: baseJob.title,
  company: baseJob.company,
  website_domain: "jobs.example",
  ats_platform: "workday",
  success: true,
  error_message: null,
  started_at: "2026-08-03T09:00:00Z",
  finished_at: "2026-08-03T09:02:00Z",
  duration_seconds: 120,
  step_count: 8,
  memories_injected: 3,
  memories_extracted: 1,
  cost_usd: 0.12,
  cost_breakdown: {
    classification: 0.01,
    cover_letter: 0.02,
    cv: 0.01,
    application_agent: 0.05,
    judge: 0.02,
    memory_extract: 0.01,
  },
  created_at: "2026-08-03T09:02:00Z",
};

describe("ReviewApplyTab", () => {
  beforeEach(() => {
    mockGetJobs.mockResolvedValue([structuredClone(baseJob)] as never);
    mockGetMetrics.mockResolvedValue([] as never);
    mockGetApplyStatus.mockResolvedValue({ running: false, log: [] } as never);
    mockGetAutomation.mockResolvedValue({
      config: { enabled: false },
      status: { state: "disabled", last_cycle_finished_at: null },
    } as never);
    mockUpdateCategory.mockResolvedValue({ success: true, job: baseJob } as never);
    mockUpdateCategories.mockResolvedValue({ success: true, updated: 1, missing: [] } as never);
    mockStartApplying.mockResolvedValue({ success: true, message: "started" } as never);
    mockStopApplying.mockResolvedValue({ success: true } as never);
    mockGetAttempt.mockResolvedValue({
      manifest: {
        attempt_id: "attempt-newest",
        status: "applied",
        job_url: baseJob.url,
        job_title: baseJob.title,
        company: baseJob.company,
        started_at: metric.started_at,
        finished_at: metric.finished_at,
        duration_seconds: metric.duration_seconds,
        step_count: metric.step_count,
        agent_claimed_success: true,
        judge_verdict: true,
        submission_confirmed: true,
        judgement: { reasoning: "Confirmation page was visible." },
        confirmation_evidence: "Application submitted",
        error: null,
        errors: [],
        recording_error: null,
        cost_usd: metric.cost_usd,
        cost_breakdown: metric.cost_breakdown || null,
      },
      steps: [],
      screenshots: [],
      recording: null,
      files: [],
    });
    mockGetAttemptFile.mockResolvedValue(new Blob(["file"]));
    mockOpenCoverLetter.mockResolvedValue({ success: true } as never);
    mockOpenResume.mockResolvedValue({ success: true } as never);
    mockResolveApplicationOutcome.mockResolvedValue({ success: true } as never);
    mockTailorResumes.mockResolvedValue({
      success: true,
      results: [{ url: baseJob.url, status: "done" }],
    } as never);
    mockExport.mockResolvedValue("title,company\nBackend Engineer,Acme\n");
    vi.stubGlobal("alert", vi.fn());
    vi.stubGlobal("confirm", vi.fn(() => true));
  });

  it("uses the same list and row when the selected category changes", async () => {
    const onJobsChanged = vi.fn();
    const { rerender } = render(
      <ReviewApplyTab category="qualified" onJobsChanged={onJobsChanged} />,
    );

    expect(await screen.findByText("Backend Engineer")).toBeInTheDocument();
    expect(mockGetJobs).toHaveBeenCalledWith({ category: "qualified", search: undefined });

    const appliedJob = { ...baseJob, status: "applied", category: "applied" } as Job;
    mockGetJobs.mockResolvedValue([appliedJob] as never);
    rerender(<ReviewApplyTab category="applied" onJobsChanged={onJobsChanged} />);

    await waitFor(() => expect(mockGetJobs).toHaveBeenCalledWith({ category: "applied", search: undefined }));
    expect(screen.getByText("Backend Engineer")).toBeInTheDocument();
  });

  it("sorts past jobs by their displayed application date, newest first", async () => {
    const older = {
      ...baseJob,
      url: "https://jobs.example/older",
      title: "Older application",
      status: "applied",
      category: "applied",
      applied_at: "2026-08-02T09:00:00Z",
      collected_at: "2026-08-02T09:00:00Z",
    } as Job;
    const newer = {
      ...older,
      url: "https://jobs.example/newer",
      title: "Newer application",
      applied_at: "2026-08-04T09:00:00Z",
      collected_at: "2026-08-01T09:00:00Z",
    } as Job;
    mockGetJobs.mockResolvedValue([older, newer] as never);

    render(<ReviewApplyTab category="applied" onJobsChanged={vi.fn()} />);

    const titles = await screen.findAllByText(/application$/);
    expect(titles.map((title) => title.textContent)).toEqual([
      "Newer application",
      "Older application",
    ]);
  });

  it("filters historical jobs by recency and can return to all", async () => {
    const user = userEvent.setup();
    const localDate = (daysAgo: number) => {
      const date = new Date();
      date.setDate(date.getDate() - daysAgo);
      date.setHours(12, 0, 0, 0);
      return date.toISOString();
    };
    mockGetJobs.mockResolvedValue([
      { ...baseJob, url: "today", title: "Today's application", status: "applied", category: "applied", applied_at: localDate(0) },
      { ...baseJob, url: "week", title: "Week application", status: "applied", category: "applied", applied_at: localDate(3) },
      { ...baseJob, url: "month", title: "Month application", status: "applied", category: "applied", applied_at: localDate(14) },
      { ...baseJob, url: "older", title: "Older application", status: "applied", category: "applied", applied_at: localDate(45) },
    ] as never);

    render(<ReviewApplyTab category="applied" onJobsChanged={vi.fn()} />);

    expect(await screen.findByText("Today's application")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /^All\s*4$/ })).toHaveAttribute("aria-selected", "true");

    await user.click(screen.getByRole("tab", { name: /^Today\s*1$/ }));

    expect(screen.getByText("Today's application")).toBeInTheDocument();
    expect(screen.queryByText("Week application")).not.toBeInTheDocument();
    expect(screen.queryByText("Month application")).not.toBeInTheDocument();
    expect(screen.queryByText("Older application")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select All (1)" })).toBeInTheDocument();
    expect(screen.getByText("Showing 1 job")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /^All\s*4$/ }));

    expect(screen.getByText("Week application")).toBeInTheDocument();
    expect(screen.getByText("Month application")).toBeInTheDocument();
    expect(screen.getByText("Older application")).toBeInTheDocument();
  });

  it("sorts outcome folders by the outcome update time", async () => {
    const olderOutcome = {
      ...baseJob,
      url: "https://jobs.example/older-outcome",
      title: "Older outcome",
      status: "applied",
      category: "rejected",
      applied_at: "2026-08-10T09:00:00Z",
      application_status_updated_at: "2026-08-12T09:00:00Z",
    } as Job;
    const newerOutcome = {
      ...olderOutcome,
      url: "https://jobs.example/newer-outcome",
      title: "Newer outcome",
      applied_at: "2026-08-01T09:00:00Z",
      application_status_updated_at: "2026-08-13T09:00:00Z",
    } as Job;
    mockGetJobs.mockResolvedValue([olderOutcome, newerOutcome] as never);

    render(<ReviewApplyTab category="rejected" onJobsChanged={vi.fn()} />);

    const titles = await screen.findAllByText(/outcome$/);
    expect(titles.map((title) => title.textContent)).toEqual([
      "Newer outcome",
      "Older outcome",
    ]);
  });

  it("shows all twelve categories in every row menu", async () => {
    const user = userEvent.setup();
    render(<ReviewApplyTab category="qualified" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Change category for Backend Engineer" }));
    expect(screen.getByText("Backend Engineer").closest(".divide-y")).toHaveClass("overflow-visible");
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "Review",
      "Qualified",
      "Ineligible",
      "Applied",
      "Online assessment",
      "Rejected",
      "Interview",
      "Offer",
      "Accepted",
      "Refused",
      "Failed",
      "Dismissed",
    ]);
  });

  it("moves Applied to Qualified through the category endpoint and refreshes", async () => {
    const user = userEvent.setup();
    const appliedJob = { ...baseJob, status: "applied", category: "applied" } as Job;
    mockGetJobs.mockResolvedValue([appliedJob] as never);
    const onJobsChanged = vi.fn();
    render(<ReviewApplyTab category="applied" onJobsChanged={onJobsChanged} />);

    await user.click(await screen.findByRole("button", { name: "Change category for Backend Engineer" }));
    await user.click(screen.getByRole("menuitem", { name: "Qualified" }));

    await waitFor(() => {
      expect(mockUpdateCategory).toHaveBeenCalledWith(baseJob.url, "qualified");
      expect(onJobsChanged).toHaveBeenCalled();
    });
    expect(mockGetJobs).toHaveBeenCalledTimes(2);
  });

  it("keeps the row in place and shows an inline error when a move fails", async () => {
    const user = userEvent.setup();
    mockUpdateCategory.mockRejectedValue(new Error("category conflict"));
    render(<ReviewApplyTab category="qualified" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Change category for Backend Engineer" }));
    await user.click(screen.getByRole("menuitem", { name: "Applied" }));

    expect(await screen.findByText("category conflict")).toBeInTheDocument();
    expect(screen.getByText("Backend Engineer")).toBeInTheDocument();
    expect(mockGetJobs).toHaveBeenCalledTimes(1);
  });

  it("keeps an active job in its folder, disables category changes, and exposes Stop", async () => {
    const user = userEvent.setup();
    mockGetJobs.mockResolvedValue([{ ...baseJob, status: "in_progress" }] as never);
    mockGetApplyStatus.mockResolvedValue({ running: true, log: ["Submitting form"] } as never);
    render(<ReviewApplyTab category="qualified" onJobsChanged={vi.fn()} />);

    const categoryButton = await screen.findByRole("button", { name: "Change category for Backend Engineer" });
    expect(categoryButton).toBeDisabled();
    expect(await screen.findByText("Submitting form")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /controls.stop/ }));
    await waitFor(() => expect(mockStopApplying).toHaveBeenCalledTimes(1));
  });

  it("shows only the latest attempt and opens its dossier", async () => {
    const user = userEvent.setup();
    const older = { ...metric, id: 2, run_id: "attempt-older", started_at: "2026-08-02T09:00:00Z" };
    mockGetMetrics.mockResolvedValue([older, metric] as never);
    render(<ReviewApplyTab category="qualified" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "View application dossier for Backend Engineer" }));
    expect(mockGetAttempt).toHaveBeenCalledWith("attempt-newest");
    expect(await screen.findByRole("dialog", { name: "Application audit dossier" })).toBeInTheDocument();
    expect(screen.getByText("Submission")).toBeInTheDocument();
    expect(screen.getByText("Confirmed")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close dossier" }));
    await user.click(screen.getByText("Backend Engineer"));
    expect(await screen.findByText("8 steps · 2m 0s")).toBeInTheDocument();
    expect(screen.getByText("Total $0.1200")).toBeInTheDocument();
    expect(screen.getByText("Application agent")).toBeInTheDocument();
    expect(screen.getByText("Memory extract")).toBeInTheDocument();
  });

  it("returns a reviewed non-submission to Qualified without retrying", async () => {
    const user = userEvent.setup();
    const unknownJob = {
      ...baseJob,
      status: "failed",
      category: "failed",
      last_application_outcome: {
        type: "unknown_outcome",
        retryable: false,
        attempt_id: metric.run_id!,
      },
    } as Job;
    mockGetJobs.mockResolvedValue([unknownJob] as never);
    mockGetMetrics.mockResolvedValue([metric] as never);
    mockGetAttempt.mockResolvedValue({
      manifest: {
        attempt_id: metric.run_id!,
        status: "failed",
        job_url: unknownJob.url,
        job_title: unknownJob.title,
        company: unknownJob.company,
        started_at: metric.started_at,
        finished_at: metric.finished_at,
        duration_seconds: metric.duration_seconds,
        step_count: metric.step_count,
        agent_claimed_success: null,
        judge_verdict: null,
        submission_confirmed: false,
        judgement: null,
        confirmation_evidence: null,
        error: "Interrupted",
        errors: [],
        recording_error: null,
        cost_usd: metric.cost_usd,
        cost_breakdown: metric.cost_breakdown || null,
      },
      steps: [],
      screenshots: [],
      recording: null,
      files: [],
    });

    render(<ReviewApplyTab category="failed" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Review outcome" }));
    expect(await screen.findByRole("region", { name: "Resolve unknown application outcome" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Mark as not applied" }));
    expect(screen.getByText("Confirm that no application was submitted")).toBeInTheDocument();
    expect(screen.getByText(/It will not start a new application/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Move to Qualified" }));

    await waitFor(() => {
      expect(mockResolveApplicationOutcome).toHaveBeenCalledWith(
        unknownJob.url,
        metric.run_id,
        "not_submitted",
      );
      expect(mockStartApplying).not.toHaveBeenCalled();
    });
  });

  it("shows a disabled dossier icon when no dossier exists", async () => {
    render(<ReviewApplyTab category="qualified" onJobsChanged={vi.fn()} />);

    const dossierButton = await screen.findByRole("button", {
      name: "View application dossier for Backend Engineer",
    });
    expect(dossierButton).toBeDisabled();
    expect(dossierButton.querySelector(".lucide-file-search")).toBeInTheDocument();
  });

  it("keeps apply and search available for actionable rows", async () => {
    const user = userEvent.setup();
    render(<ReviewApplyTab category="qualified" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Apply" }));
    expect(mockStartApplying).toHaveBeenCalledWith({
      job_url: baseJob.url,
      workers: 1,
      mode: "all",
      linkedin_outreach_enabled: false,
    });

    await user.type(screen.getByPlaceholderText("searchPlaceholder"), "python");
    await user.click(screen.getByRole("button", { name: "search" }));
    await waitFor(() => expect(mockGetJobs).toHaveBeenCalledWith({ category: "qualified", search: "python" }));
  });

  it("can enable LinkedIn outreach for a manual application", async () => {
    const user = userEvent.setup();
    render(<ReviewApplyTab category="qualified" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByRole("checkbox", { name: "LinkedIn outreach after confirmed application" }));
    await user.click(screen.getByRole("button", { name: "Apply" }));

    expect(mockStartApplying).toHaveBeenCalledWith({
      job_url: baseJob.url,
      workers: 1,
      mode: "all",
      linkedin_outreach_enabled: true,
    });
  });

  it("retries a failed job with a custom max-step override", async () => {
    const user = userEvent.setup();
    mockGetJobs.mockResolvedValue([{
      ...baseJob,
      status: "failed",
      category: "failed",
    }] as never);
    render(<ReviewApplyTab category="failed" onJobsChanged={vi.fn()} />);

    const maxSteps = await screen.findByRole("spinbutton", {
      name: "Maximum steps for retrying Backend Engineer",
    });
    expect(maxSteps).toHaveValue(100);
    await user.clear(maxSteps);
    await user.type(maxSteps, "125");
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(mockStartApplying).toHaveBeenCalledWith({
      job_url: baseJob.url,
      workers: 1,
      mode: "all",
      linkedin_outreach_enabled: false,
      max_steps: 125,
    });
  });

  it("keeps batch tailoring available for actionable jobs", async () => {
    const user = userEvent.setup();
    render(<ReviewApplyTab category="qualified" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByRole("checkbox", { name: "Select Backend Engineer" }));
    await user.click(screen.getByRole("button", { name: "Tailor Resumes" }));

    await waitFor(() => expect(mockTailorResumes).toHaveBeenCalledWith([baseJob.url]));
  });

  it("bulk moves failed applications to Dismissed", async () => {
    const user = userEvent.setup();
    const failedJobs = [
      { ...baseJob, status: "failed", category: "failed" },
      {
        ...baseJob,
        url: "https://jobs.example/frontend",
        title: "Frontend Engineer",
        status: "failed",
        category: "failed",
      },
    ] as Job[];
    mockGetJobs.mockResolvedValue(failedJobs as never);
    render(<ReviewApplyTab category="failed" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Select All (2)" }));
    await user.click(screen.getByRole("button", { name: "Move to…" }));
    await user.click(screen.getByRole("menuitem", { name: "Dismissed" }));

    await waitFor(() => expect(mockUpdateCategories).toHaveBeenCalledWith(
      failedJobs.map((job) => job.url),
      "blocked",
    ));
  });

  it("drops selected jobs that disappear after a search", async () => {
    const user = userEvent.setup();
    render(<ReviewApplyTab category="qualified" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByRole("checkbox", { name: "Select Backend Engineer" }));
    expect(screen.getByRole("button", { name: "Tailor Resumes" })).toBeInTheDocument();

    mockGetJobs.mockResolvedValue([] as never);
    await user.type(screen.getByPlaceholderText("searchPlaceholder"), "missing");
    await user.click(screen.getByRole("button", { name: "search" }));

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Tailor Resumes" })).not.toBeInTheDocument();
    });
  });

  it("opens preserved tailored materials from the shared expanded row", async () => {
    const user = userEvent.setup();
    mockGetJobs.mockResolvedValue([{
      ...baseJob,
      tailored_resume_path: "/materials/cv.pdf",
      cover_letter_path: "/materials/letter.txt",
      cover_letter_pdf_path: "/materials/letter.pdf",
    }] as never);
    render(<ReviewApplyTab category="qualified" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByText("Backend Engineer"));
    await user.click(screen.getByRole("button", { name: "Tailored CV" }));
    await user.click(screen.getByRole("button", { name: "Cover Letter" }));

    expect(mockOpenResume).toHaveBeenCalledWith(baseJob.url);
    expect(mockOpenCoverLetter).toHaveBeenCalledWith(baseJob.url);
  });

  it("shows the source Gmail link for an automatically recorded outcome", async () => {
    const user = userEvent.setup();
    mockGetJobs.mockResolvedValue([{
      ...baseJob,
      status: "applied",
      category: "interview",
      application_status: "interview",
      application_status_source: "gmail",
      application_status_evidence: {
        source: "gmail",
        message_id: "message-1",
        thread_id: "thread-1",
        received_at: "2026-08-20T12:00:00Z",
        gmail_url: "https://mail.google.com/mail/u/0/#all/thread-1",
        outcome: "interview",
        match_method: "company",
        reason: "Explicit invitation",
        evidence: "invite you to an interview",
        subject: "Interview invitation",
      },
    }] as never);
    render(<ReviewApplyTab category="interview" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByText("Backend Engineer"));

    const link = screen.getByRole("link", { name: "Open source email" });
    expect(link).toHaveAttribute("href", "https://mail.google.com/mail/u/0/#all/thread-1");
    expect(screen.getByText("invite you to an interview")).toBeInTheDocument();
  });

  it("shows the full status history with timestamps and sources", async () => {
    const user = userEvent.setup();
    mockGetJobs.mockResolvedValue([{
      ...baseJob,
      status_history: [
        { status: "qualified", changed_at: "2026-08-01T09:05:00Z", source: "classification" },
        { status: "applied", changed_at: "2026-08-03T09:00:00Z", source: "automation" },
        { status: "online_assessment", changed_at: "2026-08-05T10:00:00Z", source: "gmail" },
        { status: "rejected", changed_at: "2026-08-08T11:00:00Z", source: "gmail" },
      ],
    }] as never);
    render(<ReviewApplyTab category="qualified" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByText("Backend Engineer"));

    expect(screen.getByText("Status history")).toBeInTheDocument();
    expect(screen.getByText("Online assessment")).toBeInTheDocument();
    expect(screen.getByText("Rejected")).toBeInTheDocument();
    expect(screen.getAllByText(/Gmail$/)).toHaveLength(2);
  });

  it("exports all jobs from the shared toolbar", async () => {
    const user = userEvent.setup();
    const createObjectURL = vi.fn(() => "blob:jobs");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<ReviewApplyTab category="qualified" onJobsChanged={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Export CSV" }));

    await waitFor(() => expect(mockExport).toHaveBeenCalled());
    expect(createObjectURL).toHaveBeenCalled();
    expect(click).toHaveBeenCalled();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:jobs");
  });
});
