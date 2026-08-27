import { useCallback, useEffect, useRef, useState } from "react";
import {
  Brain,
  Briefcase,
  CheckSquare,
  ChevronDown,
  ChevronRight,
  Download,
  ExternalLink,
  FileSearch,
  FileText,
  Loader2,
  MoveRight,
  Play,
  Search,
  Square,
  Terminal,
  Trash2,
  Wand2,
  Zap,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  deleteJobs,
  exportJobsCsv,
  getAutomation,
  getApplyStatus,
  getJobs,
  getMetricRuns,
  openCoverLetterFile,
  openTailoredResumePdf,
  resolveApplicationOutcome,
  startApplying,
  stopApplying,
  tailorResumes,
  updateJobCategory,
  updateJobCategories,
} from "../../lib/api";
import {
  JOB_CATEGORY_OPTIONS,
  type ApplicationOutcomeDecision,
  type CostBreakdown,
  type Job,
  type JobCategory,
  type RunMetric,
} from "../../lib/types";
import JobClassificationPanel from "../../components/jobs/JobClassificationPanel";
import DossierViewer from "../../components/jobs/DossierViewer";
import LogLine from "../../components/LogLine";

const CATEGORY_STYLES: Record<JobCategory, string> = {
  review: "bg-secondary text-muted-foreground",
  qualified: "bg-success/12 text-success",
  unqualified: "bg-secondary text-muted-foreground",
  applied: "bg-primary/10 text-primary",
  online_assessment: "bg-primary/10 text-primary",
  rejected: "bg-destructive/10 text-destructive",
  interview: "bg-primary/10 text-primary",
  offer: "bg-warning/12 text-warning",
  accepted: "bg-success/12 text-success",
  refused: "bg-secondary text-muted-foreground",
  failed: "bg-destructive/10 text-destructive",
  blocked: "bg-secondary text-muted-foreground",
};

const PAST_JOB_CATEGORIES = new Set<JobCategory>([
  "unqualified",
  "applied",
  "online_assessment",
  "rejected",
  "interview",
  "offer",
  "accepted",
  "refused",
  "failed",
  "blocked",
]);

const EXCLUDED_CATEGORIES = new Set<JobCategory>(["unqualified", "blocked"]);

const OUTCOME_CATEGORIES = new Set<JobCategory>([
  "online_assessment",
  "rejected",
  "interview",
  "offer",
  "accepted",
  "refused",
]);

const RECENCY_LABELS = {
  today: "Today",
  week: "Last 7 days",
  month: "Last 30 days",
  older: "Older",
  undated: "Undated",
} as const;

type RecencyGroup = keyof typeof RECENCY_LABELS;
type RecencyFilter = "all" | RecencyGroup;

const RECENCY_FILTERS: Array<{ value: RecencyFilter; label: string }> = [
  { value: "all", label: "All" },
  ...Object.entries(RECENCY_LABELS).map(([value, label]) => ({
    value: value as RecencyGroup,
    label,
  })),
];

function jobActivityDate(job: Job, metric: RunMetric | undefined, category: JobCategory): string {
  if (OUTCOME_CATEGORIES.has(category)) {
    return job.application_status_updated_at || job.applied_at || metric?.started_at || job.collected_at || "";
  }
  return job.applied_at || metric?.started_at || job.collected_at || "";
}

function recencyGroup(date: string, now: number): RecencyGroup {
  const timestamp = Date.parse(date);
  if (!Number.isFinite(timestamp)) return "undated";

  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  if (timestamp >= startOfToday.getTime()) return "today";
  if (timestamp >= now - 7 * 24 * 60 * 60 * 1000) return "week";
  if (timestamp >= now - 30 * 24 * 60 * 60 * 1000) return "month";
  return "older";
}

function isApplicationActionable(job: Job): boolean {
  return (job.status === "pending" || job.status === "failed")
    && !EXCLUDED_CATEGORIES.has(job.category);
}

const COST_COMPONENTS: Array<[keyof CostBreakdown, string]> = [
  ["classification", "Classification"],
  ["cover_letter", "Cover letter"],
  ["outreach", "Outreach message"],
  ["cv", "CV"],
  ["application_agent", "Application agent"],
  ["judge", "Judge"],
  ["memory_extract", "Memory extract"],
];

interface ReviewApplyTabProps {
  category: JobCategory;
  onJobsChanged: () => void;
}

function categoryLabel(category: JobCategory): string {
  return JOB_CATEGORY_OPTIONS.find((option) => option.value === category)?.label || category;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function formatDate(date: string | undefined): string {
  if (!date) return "—";
  return new Date(date).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function historySourceLabel(source: string): string {
  return source === "gmail" ? "Gmail" : source.replace("_", " ");
}

export default function ReviewApplyTab({
  category,
  onJobsChanged,
}: ReviewApplyTabProps) {
  const { t } = useTranslation("jobs");
  const { t: tApply } = useTranslation("apply");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [metricsByUrl, setMetricsByUrl] = useState<Map<string, RunMetric>>(new Map());
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState("");
  const [recencyFilter, setRecencyFilter] = useState<RecencyFilter>("all");
  const [running, setRunning] = useState(false);
  const [applyingUrl, setApplyingUrl] = useState<string | null>(null);
  const [retryMaxSteps, setRetryMaxSteps] = useState<Record<string, string>>({});
  const [stopping, setStopping] = useState(false);
  const [log, setLog] = useState<string[]>([]);
  const [selectedJobs, setSelectedJobs] = useState<Set<string>>(new Set());
  const [tailoring, setTailoring] = useState(false);
  const [expandedUrl, setExpandedUrl] = useState<string | null>(null);
  const [categoryMenuUrl, setCategoryMenuUrl] = useState<string | null>(null);
  const [batchCategoryMenuOpen, setBatchCategoryMenuOpen] = useState(false);
  const [batchMoving, setBatchMoving] = useState(false);
  const [updatingCategoryUrl, setUpdatingCategoryUrl] = useState<string | null>(null);
  const [categoryError, setCategoryError] = useState<{ url: string; message: string } | null>(null);
  const [viewingAttempt, setViewingAttempt] = useState<{ attemptId: string; job: Job } | null>(null);
  const [exporting, setExporting] = useState(false);
  const [automationMinScore, setAutomationMinScore] = useState(70);
  const [linkedinOutreachEnabled, setLinkedinOutreachEnabled] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const categoryMenuRef = useRef<HTMLDivElement>(null);
  const batchCategoryMenuRef = useRef<HTMLDivElement>(null);
  const loadGenerationRef = useRef(0);
  const metricsByUrlRef = useRef(metricsByUrl);

  const loadJobs = useCallback(async (background = false, includeMetrics = !background) => {
    const generation = ++loadGenerationRef.current;
    if (!background) setLoading(true);
    try {
      const [jobRows, metrics] = await Promise.all([
        getJobs({ category, search: searchQuery || undefined }),
        includeMetrics ? getMetricRuns(200) : Promise.resolve(null),
      ]);
      if (generation !== loadGenerationRef.current) return;
      let latest = metricsByUrlRef.current;
      if (metrics) {
        latest = new Map<string, RunMetric>();
        for (const metric of metrics) {
          const existing = latest.get(metric.job_url);
          if (!existing || metric.started_at > existing.started_at) {
            latest.set(metric.job_url, metric);
          }
        }
      }
      const rows = [...(jobRows || [])];
      if (PAST_JOB_CATEGORIES.has(category)) {
        rows.sort((a, b) => {
          const dateA = jobActivityDate(a, latest.get(a.url), category);
          const dateB = jobActivityDate(b, latest.get(b.url), category);
          return dateB.localeCompare(dateA);
        });
      }
      setJobs(rows);
      if (metrics) {
        metricsByUrlRef.current = latest;
        setMetricsByUrl(latest);
      }
      const selectableUrls = new Set(
        rows
          .filter((job) => job.status !== "in_progress")
          .map((job) => job.url),
      );
      setSelectedJobs((current) => {
        const next = new Set([...current].filter((url) => selectableUrls.has(url)));
        return next.size === current.size ? current : next;
      });
    } catch {
      // Keep the last visible list when a background refresh fails.
    } finally {
      if (generation === loadGenerationRef.current) setLoading(false);
    }
  }, [category, searchQuery]);

  useEffect(() => {
    setSelectedJobs(new Set());
    setRecencyFilter("all");
    setExpandedUrl(null);
    setCategoryMenuUrl(null);
    setBatchCategoryMenuOpen(false);
    setCategoryError(null);
    void loadJobs();
  }, [category]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    let previousApplyRunning: boolean | null = null;
    let previousCycleFinished: string | null | undefined;

    const poll = async () => {
      let nextDelay = 15000;
      try {
        if (document.hidden) {
          nextDelay = 10000;
          return;
        }
        const [status, automation] = await Promise.all([
          getApplyStatus(),
          getAutomation().catch(() => null),
        ]);
        if (!active) return;
        setLog(status.log || []);
        setRunning(status.running);
        if (automation?.config.automation_min_score != null) {
          setAutomationMinScore(automation.config.automation_min_score);
        }
        if (previousApplyRunning !== null && previousApplyRunning !== status.running) {
          await loadJobs(true, previousApplyRunning === true && !status.running);
          if (!active) return;
          if (!status.running) setApplyingUrl(null);
          onJobsChanged();
        }
        previousApplyRunning = status.running;

        const cycleFinished = automation?.status.last_cycle_finished_at;
        if (
          previousCycleFinished !== undefined
          && cycleFinished
          && cycleFinished !== previousCycleFinished
        ) {
          await loadJobs(true, false);
          onJobsChanged();
        }
        previousCycleFinished = cycleFinished;
        nextDelay = status.running
          ? 2000
          : automation?.status.state === "running"
            ? 5000
            : 15000;
      } catch {
        nextDelay = 5000;
      } finally {
        if (active) timer = window.setTimeout(poll, nextDelay);
      }
    };
    void poll();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [loadJobs, onJobsChanged]);

  useEffect(() => {
    const element = logRef.current;
    if (!element) return;
    const nearBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
    if (nearBottom) element.scrollTop = element.scrollHeight;
  }, [log]);

  useEffect(() => {
    if (!categoryMenuUrl) return;
    const closeMenu = (event: MouseEvent) => {
      if (!categoryMenuRef.current?.contains(event.target as Node)) {
        setCategoryMenuUrl(null);
      }
    };
    document.addEventListener("mousedown", closeMenu);
    return () => document.removeEventListener("mousedown", closeMenu);
  }, [categoryMenuUrl]);

  useEffect(() => {
    if (!batchCategoryMenuOpen) return;
    const closeMenu = (event: MouseEvent) => {
      if (!batchCategoryMenuRef.current?.contains(event.target as Node)) {
        setBatchCategoryMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", closeMenu);
    return () => document.removeEventListener("mousedown", closeMenu);
  }, [batchCategoryMenuOpen]);

  const handleExport = useCallback(async () => {
    setExporting(true);
    try {
      const csv = await exportJobsCsv();
      const blobUrl = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
      const link = document.createElement("a");
      link.href = blobUrl;
      link.download = "langhire-jobs.csv";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(blobUrl);
    } catch {
      // Export is non-critical; leave the current list usable.
    } finally {
      setExporting(false);
    }
  }, []);

  useEffect(() => {
    const onExport = () => void handleExport();
    window.addEventListener("langhire:export", onExport);
    return () => window.removeEventListener("langhire:export", onExport);
  }, [handleExport]);

  const handleApply = async (urls: string[], maxSteps?: number) => {
    try {
      const response = await startApplying(
        urls.length === 1
          ? {
              job_url: urls[0],
              workers: 1,
              mode: "all",
              linkedin_outreach_enabled: linkedinOutreachEnabled,
              ...(maxSteps ? { max_steps: maxSteps } : {}),
            }
          : {
              job_urls: urls,
              workers: 1,
              mode: "all",
              linkedin_outreach_enabled: linkedinOutreachEnabled,
            },
      );
      if (!response.success) {
        alert(response.message);
        return;
      }
      setApplyingUrl(urls.length === 1 ? urls[0] : null);
      setSelectedJobs(new Set());
      setRunning(true);
    } catch (error) {
      alert(error instanceof Error ? error.message : "Failed to start application");
    }
  };

  const handleOutcomeResolution = async (decision: ApplicationOutcomeDecision) => {
    if (!viewingAttempt) return;
    const { attemptId, job } = viewingAttempt;
    await resolveApplicationOutcome(job.url, attemptId, decision);
    setViewingAttempt(null);
    await loadJobs(true);
    onJobsChanged();
  };

  const handleStop = async () => {
    setStopping(true);
    try {
      await stopApplying();
      setRunning(false);
      setApplyingUrl(null);
      await loadJobs(true);
      onJobsChanged();
    } catch (error) {
      alert(error instanceof Error ? error.message : "Failed to stop application");
    } finally {
      setStopping(false);
    }
  };

  const handleCategoryChange = async (job: Job, nextCategory: JobCategory) => {
    if (nextCategory === job.category) {
      setCategoryMenuUrl(null);
      return;
    }
    setUpdatingCategoryUrl(job.url);
    setCategoryError(null);
    try {
      await updateJobCategory(job.url, nextCategory);
      setCategoryMenuUrl(null);
      await loadJobs(true);
      onJobsChanged();
    } catch (error) {
      setCategoryError({
        url: job.url,
        message: error instanceof Error ? error.message : "Failed to change category",
      });
    } finally {
      setUpdatingCategoryUrl(null);
    }
  };

  const handleOpenResume = async (job: Job) => {
    if (!job.tailored_resume_path) return;
    try {
      await openTailoredResumePdf(job.url);
    } catch (error) {
      alert(error instanceof Error ? error.message : "Failed to open tailored CV");
    }
  };

  const handleOpenCoverLetter = async (job: Job) => {
    if (!job.cover_letter_pdf_path && !job.cover_letter_path) return;
    try {
      await openCoverLetterFile(job.url);
    } catch (error) {
      alert(error instanceof Error ? error.message : "Failed to open cover letter");
    }
  };

  const showRecencyFilters = PAST_JOB_CATEGORIES.has(category);
  const recencyGroups = showRecencyFilters
    ? jobs.map((job) => recencyGroup(jobActivityDate(job, metricsByUrl.get(job.url), category), Date.now()))
    : [];
  const recencyCounts = recencyGroups.reduce<Partial<Record<RecencyGroup, number>>>((counts, group) => {
    counts[group] = (counts[group] || 0) + 1;
    return counts;
  }, {});
  const visibleJobs = showRecencyFilters && recencyFilter !== "all"
    ? jobs.filter((_, index) => recencyGroups[index] === recencyFilter)
    : jobs;
  const selectableJobs = visibleJobs.filter((job) => job.status !== "in_progress");
  const selectedJobRows = visibleJobs.filter((job) => selectedJobs.has(job.url));
  const selectedJobsCanApply = selectedJobRows.length > 0
    && selectedJobRows.every(isApplicationActionable);
  const allSelectableSelected = selectableJobs.length > 0
    && selectableJobs.every((job) => selectedJobs.has(job.url));

  const selectRecencyFilter = (filter: RecencyFilter) => {
    setRecencyFilter(filter);
    setSelectedJobs(new Set());
    setExpandedUrl(null);
  };

  const toggleSelected = (url: string) => {
    setSelectedJobs((current) => {
      const next = new Set(current);
      if (next.has(url)) next.delete(url);
      else next.add(url);
      return next;
    });
  };

  const toggleSelectAll = () => {
    setSelectedJobs(
      allSelectableSelected ? new Set() : new Set(selectableJobs.map((job) => job.url)),
    );
  };

  const handleBatchCategoryChange = async (nextCategory: JobCategory) => {
    if (!selectedJobs.size || nextCategory === category) {
      setBatchCategoryMenuOpen(false);
      return;
    }
    setBatchMoving(true);
    try {
      await updateJobCategories([...selectedJobs], nextCategory);
      setSelectedJobs(new Set());
      setBatchCategoryMenuOpen(false);
      await loadJobs(true);
      onJobsChanged();
    } catch (error) {
      alert(error instanceof Error ? error.message : "Failed to move selected jobs");
    } finally {
      setBatchMoving(false);
    }
  };

  const handleBatchTailor = async () => {
    if (!selectedJobs.size) return;
    setTailoring(true);
    try {
      const response = await tailorResumes([...selectedJobs]);
      const done = response.results.filter((result) => result.status === "done").length;
      const failed = response.results.filter((result) => result.status === "error").length;
      alert(`Tailored ${done} resume(s)${failed ? `, ${failed} failed` : ""}`);
      setSelectedJobs(new Set());
      await loadJobs(true);
    } catch (error) {
      alert(error instanceof Error ? error.message : "Failed to tailor resumes");
    } finally {
      setTailoring(false);
    }
  };

  const handleBatchDelete = async () => {
    if (!selectedJobs.size) return;
    if (!confirm(`Delete ${selectedJobs.size} selected job(s)? This cannot be undone.`)) return;
    try {
      await deleteJobs([...selectedJobs]);
      setSelectedJobs(new Set());
      await loadJobs(true);
      onJobsChanged();
    } catch (error) {
      alert(error instanceof Error ? error.message : "Failed to delete jobs");
    }
  };

  return (
    <div>
      {(log.length > 0 || running) && (
        <div className="card mb-5">
          <div className="mb-4 flex items-center justify-between">
            <h3 className="section-title">
              {running ? tApply("log.liveOutput") : tApply("log.outputLog")}
            </h3>
            {running && (
              <button
                type="button"
                onClick={handleStop}
                disabled={stopping}
                className="btn-destructive px-3 py-1.5 text-xs disabled:opacity-50"
              >
                {stopping ? <Loader2 className="h-3 w-3 animate-spin" /> : <Square className="h-3 w-3" />}
                {stopping ? "Stopping…" : tApply("controls.stop")}
              </button>
            )}
          </div>
          <div ref={logRef} className="log-viewer">
            <div className="mb-2 flex items-center gap-2 text-muted-foreground">
              <Terminal className="h-3.5 w-3.5" /> {tApply("log.applicationLog")}
              {running && <Loader2 className="h-3.5 w-3.5 animate-spin text-green-400" />}
            </div>
            {log.map((line, index) => <LogLine key={index} line={line} />)}
          </div>
        </div>
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void loadJobs();
        }}
        className="mb-5 flex flex-col gap-3 rounded-xl border border-border bg-card p-3 sm:flex-row"
      >
          <div className="relative flex-1">
            <Search className="absolute left-4 top-3.5 h-4 w-4 text-muted-foreground" />
            <input
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder={t("searchPlaceholder")}
              className="input-base !pl-10"
            />
          </div>
          <div className="flex gap-2">
          <button type="submit" className="btn-primary">{t("search")}</button>
          <button
            type="button"
            onClick={() => void handleExport()}
            disabled={exporting}
            className="btn-secondary disabled:opacity-50"
            title="Export all jobs as CSV"
          >
            {exporting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
            Export CSV
          </button>
          </div>
      </form>

      {!loading && showRecencyFilters && jobs.length > 0 && (
        <div role="tablist" aria-label="Application date" className="mb-3 flex flex-wrap gap-1.5">
          {RECENCY_FILTERS
            .filter(({ value }) => value !== "undated" || (recencyCounts.undated || 0) > 0)
            .map(({ value, label }) => {
              const active = recencyFilter === value;
              const count = value === "all" ? jobs.length : (recencyCounts[value] || 0);
              return (
                <button
                  key={value}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  onClick={() => selectRecencyFilter(value)}
                  className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-semibold transition-colors ${
                    active
                      ? "border-primary/30 bg-primary/10 text-primary"
                      : "border-border bg-card text-muted-foreground hover:bg-secondary hover:text-foreground"
                  }`}
                >
                  {label}
                  <span className="tabular-nums opacity-75">{count}</span>
                </button>
              );
            })}
        </div>
      )}

      {!loading && selectableJobs.length > 0 && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <button
            type="button"
            onClick={toggleSelectAll}
            className="inline-flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
          >
            <CheckSquare className="h-4 w-4" />
            {allSelectableSelected ? "Deselect All" : "Select All"} ({selectableJobs.length})
          </button>
          {selectableJobs.some(isApplicationActionable) && (
            <label className="inline-flex items-center gap-2 text-sm text-muted-foreground">
              <input
                type="checkbox"
                checked={linkedinOutreachEnabled}
                onChange={(event) => setLinkedinOutreachEnabled(event.target.checked)}
                disabled={running}
                className="h-4 w-4 rounded border-border accent-primary"
              />
              LinkedIn outreach after confirmed application
            </label>
          )}
        </div>
      )}

      {loading ? (
        <div className="flex h-48 items-center justify-center">
          <Loader2 className="h-8 w-8 animate-spin text-primary" />
        </div>
      ) : jobs.length === 0 ? (
        <div className="card py-12 text-center">
          <Briefcase className="mx-auto mb-3 h-12 w-12 text-muted-foreground" />
          <h3 className="section-title mb-1">No {categoryLabel(category).toLowerCase()} jobs</h3>
          <p className="text-[13px] text-muted-foreground">Try another folder or search.</p>
        </div>
      ) : visibleJobs.length === 0 ? (
        <div className="card py-12 text-center">
          <Briefcase className="mx-auto mb-3 h-12 w-12 text-muted-foreground" />
          <h3 className="section-title mb-1">No jobs in {recencyFilter === "all" ? "All" : RECENCY_LABELS[recencyFilter]}</h3>
          <p className="text-[13px] text-muted-foreground">Choose another date range.</p>
        </div>
      ) : (
        <div className="divide-y divide-border overflow-visible rounded-xl border border-border bg-card">
          {visibleJobs.map((job) => {
            const metric = metricsByUrl.get(job.url);
            const attemptId = metric?.run_id || null;
            const expanded = expandedUrl === job.url;
            const actionable = isApplicationActionable(job);
            const outcomeResolutionRequired = job.last_application_outcome?.type === "unknown_outcome"
              && job.last_application_outcome.retryable === false;
            const inProgress = job.status === "in_progress";
            const selectable = !inProgress;
            const updating = updatingCategoryUrl === job.url;
            const retryStepValue = retryMaxSteps[job.url] ?? "100";
            const retryStepLimit = Number(retryStepValue);
            const retryStepLimitValid = Number.isInteger(retryStepLimit)
              && retryStepLimit >= 1
              && retryStepLimit <= 500;
            return (
              <div key={job.url}>
                <div className="flex flex-wrap items-center gap-3 px-4 py-3.5 transition-colors hover:bg-secondary/45">
                  {selectable && (
                    <input
                      type="checkbox"
                      checked={selectedJobs.has(job.url)}
                      onChange={() => toggleSelected(job.url)}
                      aria-label={`Select ${job.title || "job"}`}
                      className="h-4 w-4 flex-shrink-0 cursor-pointer rounded border-border text-primary focus:ring-primary"
                    />
                  )}
                  <button
                    type="button"
                    onClick={() => setExpandedUrl(expanded ? null : job.url)}
                    className="flex min-w-[280px] flex-[1_1_420px] items-center gap-3 text-left"
                  >
                    {expanded
                      ? <ChevronDown className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
                      : <ChevronRight className="h-4 w-4 flex-shrink-0 text-muted-foreground" />}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <p className="truncate text-sm font-semibold text-foreground">{job.title || "Untitled"}</p>
                        {job.easy_apply && <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-semibold text-primary">Easy Apply</span>}
                        {job.classification?.score != null && (
                          <span className={`rounded-md px-1.5 py-0.5 text-[10px] font-semibold tabular-nums ${
                            job.classification.score >= automationMinScore
                              ? "bg-success/12 text-success"
                              : "bg-warning/12 text-warning"
                          }`}>
                            Fit {job.classification.score}
                          </span>
                        )}
                      </div>
                      <p className="truncate text-[12px] text-muted-foreground">
                        {job.company || "Unknown company"} · {job.location || "—"}
                      </p>
                      {job.category === "qualified" && job.classification?.score != null && job.classification.score < automationMinScore && (
                        <p className="mt-0.5 text-[11px] font-medium text-warning">
                          Qualified for review, below the autonomous score gate of {automationMinScore}
                        </p>
                      )}
                    </div>
                    <span className="hidden w-36 flex-shrink-0 text-right text-[12px] text-muted-foreground xl:block">
                      {formatDate(job.applied_at || metric?.started_at || job.collected_at)}
                    </span>
                    <span className="hidden w-16 flex-shrink-0 text-right text-[12px] text-muted-foreground xl:block">
                      {metric ? formatDuration(metric.duration_seconds) : "—"}
                    </span>
                  </button>

                  {inProgress && (
                    <button
                      type="button"
                      onClick={handleStop}
                      disabled={stopping}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-primary/10 px-2.5 py-1.5 text-[11px] font-semibold text-primary disabled:opacity-50"
                    >
                      {stopping ? <Loader2 className="h-3 w-3 animate-spin" /> : <Square className="h-3 w-3" />}
                      In Progress
                    </button>
                  )}

                  <div
                    ref={categoryMenuUrl === job.url ? categoryMenuRef : undefined}
                    className="relative flex-shrink-0"
                  >
                    <button
                      type="button"
                      onClick={() => setCategoryMenuUrl(categoryMenuUrl === job.url ? null : job.url)}
                      disabled={inProgress || updating}
                      aria-label={`Change category for ${job.title || "job"}`}
                      aria-haspopup="menu"
                      aria-expanded={categoryMenuUrl === job.url}
                      className={`inline-flex min-w-[100px] items-center justify-center gap-1 rounded-lg px-2.5 py-1.5 text-[11px] font-semibold disabled:cursor-not-allowed disabled:opacity-60 ${CATEGORY_STYLES[job.category]}`}
                    >
                      {updating
                        ? <Loader2 className="h-3 w-3 animate-spin" />
                        : <>{categoryLabel(job.category)}<ChevronDown className="h-3 w-3" /></>}
                    </button>
                    {categoryMenuUrl === job.url && (
                      <div role="menu" className="absolute right-0 z-30 mt-1.5 w-40 rounded-lg border border-border bg-card p-1 shadow-lg">
                        {JOB_CATEGORY_OPTIONS.map((option) => (
                          <button
                            key={option.value}
                            type="button"
                            role="menuitem"
                            disabled={option.value === job.category}
                            onClick={() => void handleCategoryChange(job, option.value)}
                            className="flex w-full rounded-md px-2.5 py-1.5 text-left text-xs text-foreground transition-colors hover:bg-secondary disabled:cursor-default disabled:font-semibold disabled:opacity-50"
                          >
                            {option.label}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>

                  {actionable && !outcomeResolutionRequired && (
                    <div className="flex items-center gap-2">
                      {job.status === "failed" && (
                        <label className="flex items-center gap-1 text-[11px] text-muted-foreground">
                          Max steps
                          <input
                            type="number"
                            min={1}
                            max={500}
                            value={retryStepValue}
                            onChange={(event) => setRetryMaxSteps((current) => ({
                              ...current,
                              [job.url]: event.target.value,
                            }))}
                            aria-label={`Maximum steps for retrying ${job.title || "job"}`}
                            title="Override the default 70-step limit for this retry"
                            className="h-7 w-16 rounded-md border border-border bg-card px-2 text-xs text-foreground"
                          />
                        </label>
                      )}
                      <button
                        type="button"
                        onClick={() => void handleApply(
                          [job.url],
                          job.status === "failed" ? retryStepLimit : undefined,
                        )}
                        disabled={running || (job.status === "failed" && !retryStepLimitValid)}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-foreground px-3 py-1.5 text-[11px] font-semibold text-background disabled:opacity-40"
                      >
                        {applyingUrl === job.url ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />}
                        {job.status === "failed" ? "Retry" : "Apply"}
                      </button>
                    </div>
                  )}

                  {outcomeResolutionRequired && attemptId && (
                    <button
                      type="button"
                      onClick={() => setViewingAttempt({ attemptId, job })}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-warning/30 bg-warning/10 px-3 py-1.5 text-[11px] font-semibold text-warning hover:bg-warning/15"
                    >
                      <FileSearch className="h-3.5 w-3.5" /> Review outcome
                    </button>
                  )}

                  <button
                    type="button"
                    onClick={() => attemptId && setViewingAttempt({ attemptId, job })}
                    disabled={!attemptId}
                    aria-label={`View application dossier for ${job.title || "job"}`}
                    title={attemptId ? "View latest application dossier" : "No application dossier"}
                    className="rounded-md p-2 text-muted-foreground transition-colors hover:bg-primary/5 hover:text-primary disabled:cursor-not-allowed disabled:opacity-30"
                  >
                    <FileSearch className="h-4 w-4" />
                  </button>
                  <a
                    href={job.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={`Open ${job.title || "job"}`}
                    className="rounded-md p-2 text-muted-foreground transition-colors hover:text-primary"
                  >
                    <ExternalLink className="h-4 w-4" />
                  </a>
                </div>

                {categoryError?.url === job.url && (
                  <p className="border-t border-border bg-destructive/10 px-4 py-2 text-xs text-destructive">{categoryError.message}</p>
                )}

                {expanded && (
                  <div className="border-t border-border bg-secondary px-4 py-4">
                    <div className="grid gap-4 md:grid-cols-2">
                      <div className="space-y-3">
                        <div>
                          <p className="text-[11px] font-medium uppercase text-muted-foreground">Job URL</p>
                          <a href={job.url} target="_blank" rel="noopener noreferrer" className="block truncate text-sm text-primary hover:underline">{job.url}</a>
                        </div>
                        <div>
                          <p className="text-[11px] font-medium uppercase text-muted-foreground">Automation status</p>
                          <p className="text-sm text-foreground">{job.status.replace("_", " ")}</p>
                          {(job.error || metric?.error_message) && <p className="mt-0.5 text-xs text-red-600">{job.error || metric?.error_message}</p>}
                        </div>
                        <div>
                          <p className="text-[11px] font-medium uppercase text-muted-foreground">Dates</p>
                          <p className="text-sm text-foreground">Collected {formatDate(job.collected_at)}</p>
                          <p className="text-sm text-foreground">Last applied {formatDate(job.applied_at || metric?.started_at)}</p>
                        </div>
                        {job.status_history && job.status_history.length > 0 && (
                          <div>
                            <p className="text-[11px] font-medium uppercase text-muted-foreground">Status history</p>
                            <ol className="mt-2 space-y-2 border-l border-border pl-3">
                              {job.status_history.map((entry, index) => (
                                <li key={`${entry.status}-${entry.changed_at}-${index}`} className="relative">
                                  <span className="absolute -left-[15px] top-1.5 h-1.5 w-1.5 rounded-full bg-primary" />
                                  <p className="text-sm font-medium text-foreground">{categoryLabel(entry.status)}</p>
                                  <p className="text-xs text-muted-foreground">
                                    {formatDate(entry.changed_at)} · {historySourceLabel(entry.source)}
                                  </p>
                                </li>
                              ))}
                            </ol>
                          </div>
                        )}
                        {job.application_status_evidence?.source === "gmail" && job.application_status_evidence.gmail_url && (
                          <div>
                            <p className="text-[11px] font-medium uppercase text-muted-foreground">Gmail outcome evidence</p>
                            <p className="mt-0.5 text-sm text-foreground">{job.application_status_evidence.evidence}</p>
                            <a
                              href={job.application_status_evidence.gmail_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="mt-1 inline-flex items-center gap-1 text-xs font-semibold text-primary hover:underline"
                            >
                              <ExternalLink className="h-3 w-3" /> Open source email
                            </a>
                          </div>
                        )}
                        {(job.tailored_resume_path || job.cover_letter_pdf_path || job.cover_letter_path) && (
                          <div>
                            <p className="text-[11px] font-medium uppercase text-muted-foreground">Application files</p>
                            <div className="mt-1.5 flex flex-wrap gap-2">
                              {job.tailored_resume_path && (
                                <button type="button" onClick={() => void handleOpenResume(job)} className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-semibold hover:text-primary">
                                  <FileText className="h-3.5 w-3.5" /> Tailored CV
                                </button>
                              )}
                              {(job.cover_letter_pdf_path || job.cover_letter_path) && (
                                <button type="button" onClick={() => void handleOpenCoverLetter(job)} className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-semibold hover:text-primary">
                                  <FileText className="h-3.5 w-3.5" /> Cover Letter
                                </button>
                              )}
                            </div>
                          </div>
                        )}
                      </div>
                      {metric ? (
                        <div className="space-y-3">
                          <div className="flex items-start gap-2"><Brain className="mt-0.5 h-3.5 w-3.5 text-muted-foreground" /><div><p className="text-[11px] font-medium uppercase text-muted-foreground">Memories</p><p className="text-sm">{metric.memories_injected} injected · {metric.memories_extracted} extracted</p></div></div>
                          <div className="flex items-start gap-2">
                            <Zap className="mt-0.5 h-3.5 w-3.5 text-muted-foreground" />
                            <div className="min-w-48">
                              <p className="text-[11px] font-medium uppercase text-muted-foreground">Latest attempt</p>
                              <p className="text-sm">{metric.step_count} steps · {formatDuration(metric.duration_seconds)}</p>
                              {metric.cost_usd != null && (
                                <p className="mt-1 text-sm font-semibold">Total ${metric.cost_usd.toFixed(4)}</p>
                              )}
                              {metric.cost_breakdown && (
                                <div className="mt-1.5 space-y-0.5 border-t border-border pt-1.5">
                                  {COST_COMPONENTS.map(([key, label]) => (
                                    <div key={key} className="flex justify-between gap-4 text-xs text-muted-foreground">
                                      <span>{label}</span>
                                      <span>${(metric.cost_breakdown?.[key] ?? 0).toFixed(4)}</span>
                                    </div>
                                  ))}
                                </div>
                              )}
                            </div>
                          </div>
                        </div>
                      ) : (
                        <div className="flex items-center justify-center text-sm text-muted-foreground">No application metrics available.</div>
                      )}
                    </div>
                    <JobClassificationPanel
                      job={job}
                      onScreeningChanged={() => {
                        void loadJobs(true);
                        onJobsChanged();
                      }}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {visibleJobs.length > 0 && (
        <p className="mt-4 text-center text-[13px] text-muted-foreground">Showing {visibleJobs.length} job{visibleJobs.length === 1 ? "" : "s"}</p>
      )}

      {selectedJobs.size > 0 && (
        <div className="fixed bottom-4 left-1/2 z-40 flex max-w-[calc(100vw-2rem)] -translate-x-1/2 flex-wrap items-center justify-center gap-3 rounded-xl border border-border bg-card/95 px-5 py-3 shadow-lg backdrop-blur">
          <span className="text-sm font-medium">{selectedJobs.size} selected</span>
          <div ref={batchCategoryMenuRef} className="relative">
            <button
              type="button"
              onClick={() => setBatchCategoryMenuOpen((open) => !open)}
              disabled={batchMoving}
              aria-haspopup="menu"
              aria-expanded={batchCategoryMenuOpen}
              className="btn-secondary px-3 py-1.5 text-sm disabled:opacity-40"
            >
              {batchMoving ? <Loader2 className="h-4 w-4 animate-spin" /> : <MoveRight className="h-4 w-4" />}
              Move to…
            </button>
            {batchCategoryMenuOpen && (
              <div role="menu" className="absolute bottom-full left-0 z-50 mb-2 w-44 rounded-lg border border-border bg-card p-1 shadow-lg">
                {JOB_CATEGORY_OPTIONS.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    role="menuitem"
                    disabled={option.value === category}
                    onClick={() => void handleBatchCategoryChange(option.value)}
                    className="flex w-full rounded-md px-2.5 py-1.5 text-left text-xs text-foreground transition-colors hover:bg-secondary disabled:cursor-default disabled:font-semibold disabled:opacity-50"
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            )}
          </div>
          {selectedJobsCanApply && (
            <>
              <button type="button" onClick={() => void handleApply([...selectedJobs])} disabled={running} className="inline-flex items-center gap-1.5 rounded-lg bg-foreground px-4 py-1.5 text-sm font-semibold text-background disabled:opacity-40"><Play className="h-4 w-4" />Apply</button>
              <button type="button" onClick={() => void handleBatchTailor()} disabled={tailoring} className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-1.5 text-sm font-semibold text-white disabled:opacity-40">{tailoring ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}Tailor Resumes</button>
            </>
          )}
          <button type="button" onClick={() => void handleBatchDelete()} className="inline-flex items-center gap-1.5 rounded-lg bg-destructive px-4 py-1.5 text-sm font-semibold text-white"><Trash2 className="h-4 w-4" />Delete</button>
          <button type="button" onClick={() => setSelectedJobs(new Set())} className="text-sm text-muted-foreground underline hover:text-foreground">Deselect All</button>
        </div>
      )}

      {viewingAttempt && (
        <DossierViewer
          attemptId={viewingAttempt.attemptId}
          outcomeResolutionRequired={
            viewingAttempt.job.last_application_outcome?.type === "unknown_outcome"
            && viewingAttempt.job.last_application_outcome.retryable === false
          }
          onResolveOutcome={handleOutcomeResolution}
          onClose={() => setViewingAttempt(null)}
        />
      )}
    </div>
  );
}
