import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CollectTab from "./CollectTab";
import {
  startJobCollection,
  stopJobCollection,
  stopJobClassification,
  getCollectionStatus,
  getPlugins,
  getProfile,
  getAutomation,
  importLinkedInJob,
  saveAutomationConfig,
  startAutomation,
  stopAutomation,
} from "../../lib/api";

// i18n: identity translator so assertions can use raw keys.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { changeLanguage: vi.fn() } }),
}));

vi.mock("../../lib/api", () => ({
  startJobCollection: vi.fn(),
  stopJobCollection: vi.fn(),
  stopJobClassification: vi.fn(),
  getCollectionStatus: vi.fn(),
  getPlugins: vi.fn(),
  getProfile: vi.fn(),
  getAutomation: vi.fn(),
  importLinkedInJob: vi.fn(),
  saveAutomationConfig: vi.fn(),
  startAutomation: vi.fn(),
  stopAutomation: vi.fn(),
}));

const mockStart = vi.mocked(startJobCollection);
const mockStop = vi.mocked(stopJobCollection);
const mockStopClassification = vi.mocked(stopJobClassification);
const mockStatus = vi.mocked(getCollectionStatus);
const mockGetPlugins = vi.mocked(getPlugins);
const mockGetProfile = vi.mocked(getProfile);
const mockGetAutomation = vi.mocked(getAutomation);
const mockImportLinkedInJob = vi.mocked(importLinkedInJob);
const mockSaveAutomation = vi.mocked(saveAutomationConfig);
const mockStartAutomation = vi.mocked(startAutomation);
const mockStopAutomation = vi.mocked(stopAutomation);

const idleStatus = {
  running: false,
  title: null,
  log: [],
  collected: 0,
  max_jobs: 0,
  classification_running: false,
  classification_total: 0,
  classification_completed: 0,
  classification_failed: 0,
  error: null,
  finished_at: null,
};

const automationConfig = {
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
};

const idleAutomation = {
  config: automationConfig,
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

const linkedinPlugin = {
  name: "linkedin",
  display_name: "LinkedIn",
  version: "1.0.0",
  author: "core",
  description: "LinkedIn",
  countries: ["US"],
  website: "x",
  requires_login: true,
  login_url: "x",
  is_builtin: true,
  enabled: true,
  filters: [
    {
      key: "date_posted",
      label: "Posted within",
      type: "select" as const,
      options: [{ value: "r604800", label: "Past week" }],
      url_param: "f_TPR",
      default: "r604800",
    },
    {
      key: "experience_level",
      label: "Experience level",
      type: "select" as const,
      options: [{ value: "2", label: "Entry level" }],
      url_param: "f_E",
      default: "",
    },
  ],
};
const indeedPlugin = { ...linkedinPlugin, name: "indeed", display_name: "Indeed" };
const glassdoorPlugin = { ...linkedinPlugin, name: "glassdoor", display_name: "Glassdoor" };
const zipRecruiterPlugin = {
  ...linkedinPlugin,
  name: "ziprecruiter",
  display_name: "ZipRecruiter",
};

async function openManualCollector(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Run manual scan" }));
  await screen.findByText("collector.title");
}

async function openRunnerPolicy(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: /Edit run policy/ }));
  await screen.findByText("Run policy");
}

describe("CollectTab", () => {
  let onJobsChanged: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.stubGlobal("alert", vi.fn());
    onJobsChanged = vi.fn();
    mockGetProfile.mockResolvedValue({ country: "US" } as never);
    mockGetPlugins.mockResolvedValue({
      success: true,
      plugins: [
        structuredClone(linkedinPlugin),
        structuredClone(indeedPlugin),
        structuredClone(glassdoorPlugin),
        structuredClone(zipRecruiterPlugin),
      ],
    } as never);
    mockStatus.mockResolvedValue(structuredClone(idleStatus) as never);
    mockStart.mockResolvedValue({ success: true, message: "started" } as never);
    mockStop.mockResolvedValue({ success: true } as never);
    mockStopClassification.mockResolvedValue({ success: true } as never);
    mockImportLinkedInJob.mockResolvedValue({ success: true } as never);
    mockGetAutomation.mockResolvedValue(structuredClone(idleAutomation) as never);
    mockSaveAutomation.mockResolvedValue({ success: true, config: automationConfig } as never);
    mockStartAutomation.mockResolvedValue({ success: true, ...idleAutomation } as never);
    mockStopAutomation.mockResolvedValue({ success: true, ...idleAutomation } as never);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the collector panel and loads source plugins", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await openManualCollector(user);
    expect(screen.getByText("collector.title")).toBeInTheDocument();
    await waitFor(() => expect(mockGetProfile).toHaveBeenCalled());
    // >1 source → source selector buttons render.
    expect(await screen.findByRole("button", { name: "LinkedIn" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Indeed" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Glassdoor" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "ZipRecruiter" })).not.toBeInTheDocument();
  });

  it("loads autonomous defaults and starts after saving the selected config", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);

    expect(await screen.findByRole("button", { name: "Start runner" })).toBeInTheDocument();
    await openRunnerPolicy(user);
    expect(screen.getByLabelText("Scan interval")).toHaveValue("15");
    expect(screen.getByLabelText("Daily jobs")).toHaveValue(10);
    expect(screen.getByRole("checkbox", { name: "linkedin" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "indeed" })).toBeChecked();
    const outreachToggle = screen.getByRole("checkbox", { name: /LinkedIn outreach/ });
    expect(outreachToggle).toBeChecked();
    await user.click(outreachToggle);
    expect(outreachToggle).not.toBeChecked();

    await user.click(screen.getByRole("button", { name: "Start runner" }));

    await waitFor(() => expect(mockSaveAutomation).toHaveBeenCalledWith({
      sources: ["linkedin", "indeed"],
      interval_minutes: 15,
      daily_job_limit: 10,
      daily_company_limit: 5,
      max_jobs_per_source: 5,
      max_applications_per_cycle: 2,
      automation_min_score: 70,
      linkedin_outreach_enabled: false,
      hours_old: 1,
      title: "",
      location: "",
      job_type: "",
      is_remote: false,
      linkedin_search_url: "",
    }));
    expect(mockStartAutomation).toHaveBeenCalled();
  });

  it("shows a paused reason and resumes autonomous mode", async () => {
    mockGetAutomation.mockResolvedValue({
      ...structuredClone(idleAutomation),
      config: { ...automationConfig, enabled: true },
      status: {
        ...idleAutomation.status,
        state: "paused",
        pause_reason: "Authentication or MFA could not be completed automatically",
      },
    } as never);
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);

    expect(await screen.findByText("Manual attention required")).toBeInTheDocument();
    expect(screen.getByText(/Authentication or MFA/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Resume runner" }));
    await waitFor(() => expect(mockStartAutomation).toHaveBeenCalled());
  });

  it("stops autonomous mode without force-stopping the active application", async () => {
    mockGetAutomation.mockResolvedValue({
      ...structuredClone(idleAutomation),
      config: { ...automationConfig, enabled: true },
      status: { ...idleAutomation.status, state: "running", phase: "applying" },
    } as never);
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);

    await user.click(await screen.findByRole("button", { name: "Stop after current job" }));
    expect(mockStopAutomation).toHaveBeenCalled();
  });

  it("starts collection without the browser automation dialog", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await openManualCollector(user);
    await screen.findByRole("button", { name: "LinkedIn" });

    await user.type(screen.getByPlaceholderText("collector.jobTitlePlaceholder"), "Engineer");
    await user.click(screen.getByRole("button", { name: /collector\.start/ }));

    await waitFor(() =>
      expect(mockStart).toHaveBeenCalledWith("Engineer", 20, "linkedin", {
        hours_old: "168",
      }),
    );
    expect(screen.queryByText("First-time login required")).not.toBeInTheDocument();
  });

  it("shows immediate feedback while the start request is pending", async () => {
    let resolveStart!: (value: { success: boolean; message: string }) => void;
    mockStart.mockReturnValue(
      new Promise((resolve) => {
        resolveStart = resolve;
      }) as never,
    );
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await openManualCollector(user);
    await screen.findByRole("button", { name: "LinkedIn" });

    await user.click(screen.getByRole("button", { name: /collector\.start/ }));
    expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "LinkedIn" })).toBeDisabled();

    resolveStart({ success: true, message: "started" });
    expect(
      await screen.findByRole("button", { name: /collector\.stop/ }),
    ).toBeEnabled();
  });

  it("alerts when starting collection rejects", async () => {
    mockStart.mockRejectedValue(new Error("network error"));
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await openManualCollector(user);
    await screen.findByRole("button", { name: "LinkedIn" });

    await user.click(screen.getByRole("button", { name: /collector\.start/ }));

    await waitFor(() => expect(window.alert).toHaveBeenCalledWith("network error"));
  });

  it("switches the active source", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await openManualCollector(user);
    const indeed = await screen.findByRole("button", { name: "Indeed" });
    await user.click(indeed);
    await waitFor(() => expect(indeed.className).toContain("border-primary"));
  });

  it("shows the JobSpy search scope controls", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await openManualCollector(user);

    expect(await screen.findByLabelText("Location")).toBeInTheDocument();
    expect(screen.getByLabelText("Hours old")).toHaveValue(168);
    expect(screen.getByLabelText("Hours old")).toHaveAttribute("min", "0.25");
    expect(screen.getByText("0.25 = 15 minutes")).toBeInTheDocument();
    expect(screen.getByLabelText("Job type")).toHaveValue("");
    expect(screen.getByRole("checkbox", { name: "Remote" })).not.toBeChecked();
    expect(screen.getByPlaceholderText("collector.maxJobsPlaceholder")).toHaveAttribute(
      "max",
      "500",
    );
  });

  it("passes location, hours old, job type, and remote to collection", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await openManualCollector(user);
    await screen.findByRole("button", { name: "LinkedIn" });

    await user.type(screen.getByLabelText("Location"), "Berlin");
    await user.clear(screen.getByLabelText("Hours old"));
    await user.type(screen.getByLabelText("Hours old"), "0.25");
    await user.selectOptions(screen.getByLabelText("Job type"), "fulltime");
    await user.click(screen.getByRole("checkbox", { name: "Remote" }));
    await user.click(screen.getByRole("button", { name: /collector\.start/ }));

    await waitFor(() =>
      expect(mockStart).toHaveBeenCalledWith(undefined, 20, "linkedin", {
        location: "Berlin",
        hours_old: "0.25",
        job_type: "fulltime",
        is_remote: "true",
      }),
    );
  });

  it("starts a personalized LinkedIn URL scrape with the requested maximum", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await openManualCollector(user);
    await screen.findByRole("button", { name: "LinkedIn" });

    const url =
      "https://www.linkedin.com/jobs/search-results/?keywords=software%20engineer&geoId=103035651";
    await user.type(
      screen.getByPlaceholderText("https://www.linkedin.com/jobs/search-results/?..."),
      url,
    );
    expect(screen.getByLabelText("Location")).toBeDisabled();
    expect(screen.getByPlaceholderText("collector.jobTitlePlaceholder")).toBeDisabled();
    await user.clear(screen.getByPlaceholderText("collector.maxJobsPlaceholder"));
    await user.type(screen.getByPlaceholderText("collector.maxJobsPlaceholder"), "10");
    await user.click(screen.getByRole("button", { name: /collector\.start/ }));

    await waitFor(() =>
      expect(mockStart).toHaveBeenCalledWith(
        undefined,
        10,
        "linkedin",
        undefined,
        url,
      ),
    );
  });

  it("omits freshness when Indeed job type or remote is selected", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await openManualCollector(user);
    await user.click(await screen.findByRole("button", { name: "Indeed" }));

    await user.selectOptions(screen.getByLabelText("Job type"), "fulltime");
    expect(screen.getByLabelText("Hours old")).toBeDisabled();
    expect(
      screen.getByText(
        "Indeed cannot combine Hours old with Job type or Remote, so freshness is not applied.",
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /collector\.start/ }));

    await waitFor(() =>
      expect(mockStart).toHaveBeenCalledWith(undefined, 20, "indeed", {
        job_type: "fulltime",
      }),
    );
  });

  it("resumes display when a collection is already running on mount", async () => {
    mockStatus.mockResolvedValue({
      ...idleStatus,
      running: true,
      collected: 5,
      log: ["line 1"],
    } as never);
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    // Stop button (destructive) shows when running.
    expect(await screen.findByRole("button", { name: /collector\.stop/ })).toBeInTheDocument();
    expect(screen.getByText("line 1")).toBeInTheDocument();
  });

  it("keeps automatic classification visible after scraping finishes", async () => {
    mockStatus.mockResolvedValue({
      ...idleStatus,
      classification_running: true,
      classification_total: 2,
      classification_completed: 1,
      log: [
        "🤖 Classifying 2/2: Data Analyst at Acme with gpt-5.4-mini",
      ],
    } as never);

    render(<CollectTab onJobsChanged={onJobsChanged} />);

    expect(
      await screen.findByRole("button", { name: /Stop classification/ }),
    ).toBeEnabled();
    expect(
      screen.getByText(
        "🤖 Classifying 2/2: Data Analyst at Acme with gpt-5.4-mini",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Classified 1 of 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "LinkedIn" })).toBeDisabled();
  });

  it("stops a running collection", async () => {
    mockStatus.mockResolvedValue({ ...idleStatus, running: true } as never);
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);

    await user.click(await screen.findByRole("button", { name: /collector\.stop/ }));
    expect(mockStop).toHaveBeenCalled();
  });

  it("stops classification separately from collection", async () => {
    mockStatus.mockResolvedValue({
      ...idleStatus,
      classification_running: true,
    } as never);
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);

    await user.click(
      await screen.findByRole("button", { name: "Stop classification" }),
    );
    expect(mockStopClassification).toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Stopping classification…" }),
    ).toBeDisabled();
  });

  it("imports a LinkedIn listing from its URL", async () => {
    const user = userEvent.setup();
    render(<CollectTab onJobsChanged={onJobsChanged} />);

    await user.click(screen.getByRole("button", { name: /Import LinkedIn URL/ }));
    await user.type(
      screen.getByLabelText("LinkedIn job URL"),
      "https://www.linkedin.com/jobs/view/4428487670",
    );
    await user.click(screen.getByRole("button", { name: "Import job" }));

    await waitFor(() =>
      expect(mockImportLinkedInJob).toHaveBeenCalledWith(
        "https://www.linkedin.com/jobs/view/4428487670",
      ),
    );
    expect(onJobsChanged).toHaveBeenCalled();
  });

  it("falls back to no-country plugins when getProfile rejects", async () => {
    const user = userEvent.setup();
    mockGetProfile.mockRejectedValue(new Error("no profile"));
    render(<CollectTab onJobsChanged={onJobsChanged} />);
    await openManualCollector(user);
    await waitFor(() => expect(mockGetPlugins).toHaveBeenCalled());
    expect(await screen.findByRole("button", { name: "LinkedIn" })).toBeInTheDocument();
  });
});
