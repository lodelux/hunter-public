import { useState, useEffect, useRef } from "react";
import {
  Check,
  ChevronDown,
  Link2,
  Loader2,
  Play,
  Plus,
  Search,
  Settings2,
  ShieldAlert,
  Square,
  Terminal,
} from "lucide-react";
import {
  getAutomation,
  startJobCollection,
  stopJobCollection,
  stopJobClassification,
  getCollectionStatus,
  getPlugins,
  getProfile,
  importLinkedInJob,
  saveAutomationConfig,
  startAutomation,
  stopAutomation,
} from "../../lib/api";
import type {
  AutomationConfig,
  AutomationResponse,
  AutomationSource,
  PluginConfig,
} from "../../lib/types";
import LogLine from "../../components/LogLine";
import { ProgressBar } from "../../components/ui";
import { useTranslation } from "react-i18next";

interface CollectTabProps {
  onJobsChanged: () => void;
}

const JOBSPY_SOURCES = new Set(["linkedin", "indeed"]);
const JOB_TYPES = [
  ["", "Any job type"],
  ["fulltime", "Full-time"],
  ["parttime", "Part-time"],
  ["contract", "Contract"],
  ["internship", "Internship"],
  ["temporary", "Temporary"],
  ["volunteer", "Volunteer"],
  ["perdiem", "Per diem"],
  ["nights", "Nights"],
  ["summer", "Summer"],
  ["other", "Other"],
] as const;
const INDEED_JOB_TYPES = new Set(["", "fulltime", "parttime", "contract", "internship"]);
const AUTOMATION_INTERVALS = [5, 10, 15, 30, 60] as const;
const DEFAULT_AUTOMATION_CONFIG: AutomationConfig = {
  enabled: false,
  sources: ["linkedin", "indeed"],
  interval_minutes: 15,
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

export default function CollectTab({ onJobsChanged }: CollectTabProps) {
  const { t } = useTranslation("jobs");

  // Collection state
  const [collecting, setCollecting] = useState(false);
  const [classifying, setClassifying] = useState(false);
  const [startingCollection, setStartingCollection] = useState(false);
  const [stoppingCollection, setStoppingCollection] = useState(false);
  const [stoppingClassification, setStoppingClassification] = useState(false);
  const [classificationTotal, setClassificationTotal] = useState(0);
  const [classificationCompleted, setClassificationCompleted] = useState(0);
  const [classificationFailed, setClassificationFailed] = useState(0);
  const [collectTitle, setCollectTitle] = useState("");
  const [collectMaxJobs, setCollectMaxJobs] = useState<number | "">(20);
  const [collectSource, setCollectSource] = useState("linkedin");
  const [collectLocation, setCollectLocation] = useState("");
  const [collectHoursOld, setCollectHoursOld] = useState<number | "">(168);
  const [collectJobType, setCollectJobType] = useState("");
  const [collectIsRemote, setCollectIsRemote] = useState(false);
  const [linkedInSearchUrl, setLinkedInSearchUrl] = useState("");
  const [availableSources, setAvailableSources] = useState<PluginConfig[]>([]);
  const [collectLog, setCollectLog] = useState<string[]>([]);
  const [collected, setCollected] = useState(0);
  const [automation, setAutomation] = useState<AutomationResponse | null>(null);
  const [automationConfig, setAutomationConfig] = useState<AutomationConfig>(DEFAULT_AUTOMATION_CONFIG);
  const [automationBusy, setAutomationBusy] = useState(false);
  const [automationError, setAutomationError] = useState<string | null>(null);
  const [showRunnerSettings, setShowRunnerSettings] = useState(false);
  const [showManualCollector, setShowManualCollector] = useState(false);
  const automationConfigLoaded = useRef(false);

  // LinkedIn URL import state
  const [showLinkedInImport, setShowLinkedInImport] = useState(false);
  const [linkedInUrl, setLinkedInUrl] = useState("");
  const [importingLinkedIn, setImportingLinkedIn] = useState(false);

  const logRef = useRef<HTMLDivElement>(null);

  const indeedWithoutFreshness =
    collectSource === "indeed" && Boolean(collectJobType || collectIsRemote);
  const availableJobTypes =
    collectSource === "indeed"
      ? JOB_TYPES.filter(([value]) => INDEED_JOB_TYPES.has(value))
      : JOB_TYPES;
  const collectionBusy = startingCollection || collecting || classifying;
  const usingLinkedInSearchUrl =
    collectSource === "linkedin" && Boolean(linkedInSearchUrl.trim());
  const automationRunning = automation?.status.state === "running";
  const automationPaused = automation?.status.state === "paused";
  const automationJobTypes = automationConfig.sources.includes("indeed")
    ? JOB_TYPES.filter(([value]) => INDEED_JOB_TYPES.has(value))
    : JOB_TYPES;

  // Load available plugins/sources
  useEffect(() => {
    getProfile()
      .then((p) => {
        const country = p.country || "US";
        getPlugins(country)
          .then((res) => {
            if (res.plugins) {
              const sources = res.plugins.filter((plugin) =>
                JOBSPY_SOURCES.has(plugin.name)
              );
              setAvailableSources(sources);
              setCollectSource((current) =>
                sources.some((source) => source.name === current)
                  ? current
                  : sources[0]?.name || current
              );
            }
          })
          .catch(() => {});
      })
      .catch(() => {
        getPlugins()
          .then((res) => {
            if (res.plugins) {
              const sources = res.plugins.filter((plugin) =>
                JOBSPY_SOURCES.has(plugin.name)
              );
              setAvailableSources(sources);
              setCollectSource((current) =>
                sources.some((source) => source.name === current)
                  ? current
                  : sources[0]?.name || current
              );
            }
          })
          .catch(() => {});
      });
  }, []);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    let previousCycleFinished: string | null | undefined;
    const refresh = async () => {
      let nextDelay = 10000;
      try {
        if (document.hidden) return;
        const response = await getAutomation();
        if (!active) return;
        setAutomation(response);
        if (!automationConfigLoaded.current) {
          setAutomationConfig(response.config);
          automationConfigLoaded.current = true;
        }
        const cycleFinished = response.status.last_cycle_finished_at;
        if (
          previousCycleFinished !== undefined
          && cycleFinished
          && cycleFinished !== previousCycleFinished
        ) {
          onJobsChanged();
        }
        previousCycleFinished = cycleFinished;
        nextDelay = response.status.state === "running" ? 2000 : 10000;
      } catch {
        // Keep the last status during transient backend failures.
        nextDelay = 5000;
      } finally {
        if (active) timer = window.setTimeout(refresh, nextDelay);
      }
    };
    void refresh();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [onJobsChanged]);

  // Poll collection status
  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    let wasRunning: boolean | undefined;
    let wasClassifying: boolean | undefined;
    const poll = async () => {
      let nextDelay = 10000;
      try {
        if (document.hidden) return;
        const s = await getCollectionStatus();
        if (!active) return;
        if (wasRunning === true && !s.running) onJobsChanged();
        if (wasClassifying === true && !s.classification_running) onJobsChanged();
        wasRunning = s.running;
        wasClassifying = s.classification_running;
        setCollecting(s.running);
        setClassifying(s.classification_running);
        setClassificationTotal(s.classification_total || 0);
        setClassificationCompleted(s.classification_completed || 0);
        setClassificationFailed(s.classification_failed || 0);
        if (!s.running) setStoppingCollection(false);
        if (!s.classification_running) setStoppingClassification(false);
        setCollectLog(s.log || []);
        setCollected(s.collected || 0);
        nextDelay = s.running || s.classification_running ? 2000 : 10000;
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
  }, [onJobsChanged]);

  // Auto-scroll log
  useEffect(() => {
    const el = logRef.current;
    if (el) {
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      if (nearBottom) el.scrollTop = el.scrollHeight;
    }
  }, [collectLog]);

  useEffect(() => {
    if (collecting || classifying) setShowManualCollector(true);
  }, [collecting, classifying]);

  const confirmStartCollect = async () => {
    setStartingCollection(true);
    try {
      if (usingLinkedInSearchUrl) {
        const res = await startJobCollection(
          undefined,
          collectMaxJobs ? Number(collectMaxJobs) : undefined,
          "linkedin",
          undefined,
          linkedInSearchUrl.trim(),
        );
        if (res.success) {
          setCollecting(true);
          setCollectLog([t("collector.startingCollection")]);
        } else {
          alert(res.message);
        }
        return;
      }

      const filters: Record<string, string> = {};
      if (collectLocation.trim()) filters.location = collectLocation.trim();
      if (collectHoursOld !== "" && !indeedWithoutFreshness) {
        filters.hours_old = String(collectHoursOld);
      }
      if (collectJobType) filters.job_type = collectJobType;
      if (collectIsRemote) filters.is_remote = "true";

      const res = await startJobCollection(
        collectTitle || undefined,
        collectMaxJobs ? Number(collectMaxJobs) : undefined,
        collectSource,
        filters
      );
      if (res.success) {
        setCollecting(true);
        setCollectLog([t("collector.startingCollection")]);
      } else {
        alert(res.message);
      }
    } catch (e) {
      alert(e instanceof Error ? e.message : "Failed to start collection");
    } finally {
      setStartingCollection(false);
    }
  };

  const handleStopCollect = async () => {
    setStoppingCollection(true);
    try {
      await stopJobCollection();
    } catch (e) {
      setStoppingCollection(false);
      alert(e instanceof Error ? e.message : "Failed to stop collection");
    }
  };

  const handleStopClassification = async () => {
    setStoppingClassification(true);
    try {
      await stopJobClassification();
    } catch (e) {
      setStoppingClassification(false);
      alert(e instanceof Error ? e.message : "Failed to stop classification");
    }
  };

  const handleLinkedInImport = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!linkedInUrl.trim()) return;
    setImportingLinkedIn(true);
    try {
      const res = await importLinkedInJob(linkedInUrl.trim());
      if (res.success) {
        setLinkedInUrl("");
        setShowLinkedInImport(false);
        onJobsChanged();
      }
    } catch (e2) {
      alert(e2 instanceof Error ? e2.message : "Failed to import LinkedIn job");
    } finally {
      setImportingLinkedIn(false);
    }
  };

  const toggleAutomationSource = (source: AutomationSource) => {
    setAutomationConfig((current) => {
      const selected = current.sources.includes(source);
      const sources = selected
        ? current.sources.filter((item) => item !== source)
        : [...current.sources, source];
      const jobType = sources.includes("indeed") && !INDEED_JOB_TYPES.has(current.job_type)
        ? ""
        : current.job_type;
      return {
        ...current,
        sources,
        job_type: jobType,
        linkedin_search_url: sources.includes("linkedin") ? current.linkedin_search_url : "",
      };
    });
  };

  const handleStartAutomation = async () => {
    setAutomationBusy(true);
    setAutomationError(null);
    try {
      const config = { ...automationConfig };
      delete (config as Partial<AutomationConfig>).enabled;
      const saved = await saveAutomationConfig(config);
      setAutomationConfig(saved.config);
      const response = await startAutomation();
      setAutomation(response);
      setAutomationConfig(response.config);
    } catch (error) {
      setAutomationError(error instanceof Error ? error.message : "Failed to start autonomous mode");
    } finally {
      setAutomationBusy(false);
    }
  };

  const handleStopAutomation = async () => {
    setAutomationBusy(true);
    setAutomationError(null);
    try {
      const response = await stopAutomation();
      setAutomation(response);
      setAutomationConfig(response.config);
    } catch (error) {
      setAutomationError(error instanceof Error ? error.message : "Failed to stop autonomous mode");
    } finally {
      setAutomationBusy(false);
    }
  };

  const handleSaveAutomationConfig = async () => {
    setAutomationBusy(true);
    setAutomationError(null);
    try {
      const config = { ...automationConfig };
      delete (config as Partial<AutomationConfig>).enabled;
      const saved = await saveAutomationConfig(config);
      setAutomationConfig(saved.config);
      setAutomation((current) => current ? { ...current, config: saved.config } : current);
      setShowRunnerSettings(false);
    } catch (error) {
      setAutomationError(error instanceof Error ? error.message : "Failed to save runner policy");
    } finally {
      setAutomationBusy(false);
    }
  };

  return (
    <div>
      <div className="card mb-5 overflow-hidden !p-0">
        <div className="flex flex-col gap-5 border-b border-border p-5 sm:flex-row sm:items-start sm:justify-between sm:p-6">
          <div>
            <div className="mb-3 flex items-center gap-2">
              <span className={`h-2.5 w-2.5 rounded-full ${
                automationRunning
                  ? "bg-success"
                  : automationPaused
                    ? "bg-warning"
                    : "bg-muted-foreground/50"
              }`} />
              <span className="eyebrow">Autonomous runner · {automation?.status.state || "stopped"}</span>
            </div>
            <h3 className="text-xl font-semibold tracking-[-0.025em] text-foreground">
              {automationRunning
                ? automation?.status.phase === "waiting" ? "Watching for the next cycle" : `Runner is ${automation?.status.phase || "working"}`
                : automationPaused ? "A decision is holding the runner" : "Set it once, let Hunter work"}
            </h3>
            <p className="mt-1.5 max-w-2xl text-sm leading-6 text-muted-foreground">
              Hunter drains its qualified queue, discovers fresh work, and applies only when fit and submission checks pass.
            </p>
          </div>
          {automationRunning ? (
            <button
              type="button"
              onClick={handleStopAutomation}
              disabled={automationBusy}
              className="btn-secondary shrink-0"
            >
              {automationBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
              Stop after current job
            </button>
          ) : (
            <button
              type="button"
              onClick={handleStartAutomation}
              disabled={automationBusy || automationConfig.sources.length === 0}
              className="btn-primary shrink-0 disabled:opacity-50"
            >
              {automationBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {automationPaused ? "Resume runner" : "Start runner"}
            </button>
          )}
        </div>

        <div className="p-5 sm:p-6">
        {automationPaused && automation?.status.pause_reason && (
          <div className="mb-5 flex gap-3 rounded-lg border border-warning/30 bg-warning/10 p-4 text-sm text-foreground">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
            <div>
              <div className="font-semibold">Manual attention required</div>
              <div className="mt-0.5 text-muted-foreground">{automation.status.pause_reason}</div>
            </div>
          </div>
        )}
        {automationError && <div className="error-banner mb-4">{automationError}</div>}

        <div className="mb-5 flex flex-col gap-4 rounded-lg bg-secondary/60 p-4 md:flex-row md:items-center md:justify-between">
          <div className="grid flex-1 grid-cols-2 gap-4 sm:grid-cols-4">
            <div><p className="eyebrow">Sources</p><p className="mt-1 text-sm font-semibold capitalize">{automationConfig.sources.join(" + ") || "None"}</p></div>
            <div><p className="eyebrow">Cadence</p><p className="mt-1 text-sm font-semibold">Every {automationConfig.interval_minutes} min</p></div>
            <div><p className="eyebrow">Minimum fit</p><p className="mt-1 text-sm font-semibold">{automationConfig.automation_min_score}+</p></div>
            <div><p className="eyebrow">Daily capacity</p><p className="mt-1 text-sm font-semibold">{automationConfig.daily_job_limit} starts</p></div>
          </div>
          <button
            type="button"
            onClick={() => setShowRunnerSettings((current) => !current)}
            disabled={automationRunning}
            className="btn-secondary shrink-0"
          >
            <Settings2 className="h-4 w-4" />
            {showRunnerSettings ? "Close policy" : "Edit run policy"}
            <ChevronDown className={`h-4 w-4 transition-transform ${showRunnerSettings ? "rotate-180" : ""}`} />
          </button>
        </div>

        {showRunnerSettings && (
        <div className="mb-5 rounded-lg border border-border bg-background/45 p-4">
          <div className="mb-4 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-semibold text-foreground">Run policy</p>
              <p className="mt-0.5 text-xs text-muted-foreground">Changes affect future cycles and are locked while the runner is active.</p>
            </div>
          </div>
        <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <div>
            <label className="block text-xs font-medium text-muted-foreground">Sources</label>
            <div className="mt-1 flex h-[38px] items-center gap-3 rounded-lg border border-border bg-card px-3">
              {(["linkedin", "indeed"] as AutomationSource[]).map((source) => (
                <label key={source} className="flex items-center gap-1.5 text-sm font-medium capitalize">
                  <input
                    type="checkbox"
                    checked={automationConfig.sources.includes(source)}
                    onChange={() => toggleAutomationSource(source)}
                    disabled={automationRunning}
                    className="accent-primary"
                  />
                  {source}
                </label>
              ))}
            </div>
          </div>
          <label className="text-xs font-medium text-muted-foreground">
            Scan interval
            <select
              value={automationConfig.interval_minutes}
              onChange={(event) => setAutomationConfig((current) => ({
                ...current,
                interval_minutes: Number(event.target.value) as AutomationConfig["interval_minutes"],
              }))}
              disabled={automationRunning}
              className="input-base !py-2 mt-1"
            >
              {AUTOMATION_INTERVALS.map((minutes) => (
                <option key={minutes} value={minutes}>Every {minutes} minutes</option>
              ))}
            </select>
          </label>
          <label className="text-xs font-medium text-muted-foreground">
            Daily jobs
            <input
              type="number"
              min={1}
              max={100}
              value={automationConfig.daily_job_limit}
              onChange={(event) => setAutomationConfig((current) => ({
                ...current,
                daily_job_limit: Math.max(1, Math.min(100, Number(event.target.value) || 1)),
              }))}
              disabled={automationRunning}
              className="input-base !py-2 mt-1"
            />
          </label>
          <label className="text-xs font-medium text-muted-foreground">
            New jobs/source
            <input
              type="number"
              min={1}
              max={500}
              value={automationConfig.max_jobs_per_source}
              onChange={(event) => setAutomationConfig((current) => ({
                ...current,
                max_jobs_per_source: Math.max(1, Math.min(500, Number(event.target.value) || 1)),
              }))}
              disabled={automationRunning}
              className="input-base !py-2 mt-1"
            />
          </label>
          <label className="text-xs font-medium text-muted-foreground">
            Jobs/company/day
            <input
              type="number"
              min={1}
              max={20}
              value={automationConfig.daily_company_limit}
              onChange={(event) => setAutomationConfig((current) => ({
                ...current,
                daily_company_limit: Math.max(1, Math.min(20, Number(event.target.value) || 1)),
              }))}
              disabled={automationRunning}
              className="input-base !py-2 mt-1"
            />
          </label>
          <label className="text-xs font-medium text-muted-foreground">
            Applications/cycle
            <input
              type="number"
              min={1}
              max={20}
              value={automationConfig.max_applications_per_cycle}
              onChange={(event) => setAutomationConfig((current) => ({
                ...current,
                max_applications_per_cycle: Math.max(1, Math.min(20, Number(event.target.value) || 1)),
              }))}
              disabled={automationRunning}
              className="input-base !py-2 mt-1"
            />
          </label>
          <label className="text-xs font-medium text-muted-foreground">
            Minimum fit score
            <input
              type="number"
              min={0}
              max={100}
              value={automationConfig.automation_min_score}
              onChange={(event) => setAutomationConfig((current) => ({
                ...current,
                automation_min_score: Math.max(0, Math.min(100, Number(event.target.value) || 0)),
              }))}
              disabled={automationRunning}
              className="input-base !py-2 mt-1"
            />
          </label>
        </div>

        <div className="mb-4 grid grid-cols-1 gap-3 md:grid-cols-4">
          <label className="text-xs font-medium text-muted-foreground">
            Search title
            <input
              value={automationConfig.title}
              onChange={(event) => setAutomationConfig((current) => ({ ...current, title: event.target.value }))}
              placeholder="Profile titles"
              disabled={automationRunning}
              className="input-base !py-2 mt-1"
            />
          </label>
          <label className="text-xs font-medium text-muted-foreground">
            Search location
            <input
              value={automationConfig.location}
              onChange={(event) => setAutomationConfig((current) => ({ ...current, location: event.target.value }))}
              placeholder="Profile locations"
              disabled={automationRunning}
              className="input-base !py-2 mt-1"
            />
          </label>
          <label className="text-xs font-medium text-muted-foreground">
            Freshness (hours)
            <input
              type="number"
              min={1}
              max={720}
              value={automationConfig.hours_old}
              onChange={(event) => setAutomationConfig((current) => ({
                ...current,
                hours_old: Math.max(1, Math.min(720, Number(event.target.value) || 1)),
              }))}
              disabled={automationRunning || (automationConfig.sources.includes("indeed") && Boolean(automationConfig.job_type || automationConfig.is_remote))}
              className="input-base !py-2 mt-1 disabled:opacity-50"
            />
          </label>
          <label className="text-xs font-medium text-muted-foreground">
            Automation job type
            <select
              value={automationConfig.job_type}
              onChange={(event) => setAutomationConfig((current) => ({ ...current, job_type: event.target.value }))}
              disabled={automationRunning}
              className="input-base !py-2 mt-1"
            >
              {automationJobTypes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
        </div>

        <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-end">
          {automationConfig.sources.includes("linkedin") && (
            <label className="flex-1 text-xs font-medium text-muted-foreground">
              LinkedIn search URL <span className="font-normal">(optional)</span>
              <input
                type="url"
                value={automationConfig.linkedin_search_url}
                onChange={(event) => setAutomationConfig((current) => ({ ...current, linkedin_search_url: event.target.value }))}
                placeholder="Use profile search when blank"
                disabled={automationRunning}
                className="input-base !py-2 mt-1"
              />
              <span className="mt-1 block font-normal text-muted-foreground">
                Autonomous scans inspect only the first 25 LinkedIn results.
              </span>
            </label>
          )}
          <label className="flex h-[38px] items-center gap-2 rounded-lg border border-border bg-card px-3 text-sm font-medium text-foreground">
            <input
              type="checkbox"
              checked={automationConfig.is_remote}
              onChange={(event) => setAutomationConfig((current) => ({ ...current, is_remote: event.target.checked }))}
              disabled={automationRunning}
              className="accent-primary"
            />
            Remote only
          </label>
          <label className="flex min-h-[38px] items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-sm font-medium text-foreground">
            <input
              type="checkbox"
              checked={automationConfig.linkedin_outreach_enabled}
              onChange={(event) => setAutomationConfig((current) => ({
                ...current,
                linkedin_outreach_enabled: event.target.checked,
              }))}
              disabled={automationRunning}
              className="accent-primary"
            />
            <span>
              LinkedIn outreach
              <span className="block text-xs font-normal text-muted-foreground">
                Message the company page after a confirmed application.
              </span>
            </span>
          </label>
        </div>
          <div className="flex justify-end gap-2 border-t border-border pt-4">
            <button type="button" onClick={() => setShowRunnerSettings(false)} className="btn-ghost">Cancel</button>
            <button
              type="button"
              onClick={() => void handleSaveAutomationConfig()}
              disabled={automationBusy || automationConfig.sources.length === 0}
              className="btn-primary"
            >
              {automationBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
              Save policy
            </button>
          </div>
        </div>
        )}

        {automation && (
          <div className="mb-5 grid gap-px overflow-hidden rounded-lg border border-border bg-border text-xs sm:grid-cols-5">
            <div className="bg-card p-3"><span className="eyebrow">Phase</span><div className="mt-1 font-semibold capitalize">{automation.status.phase}</div></div>
            <div className="bg-card p-3"><span className="eyebrow">Today</span><div className="mt-1 font-semibold">{automation.status.today_started} / {automation.status.today_limit}</div></div>
            <div className="bg-card p-3"><span className="eyebrow">Queued</span><div className="mt-1 font-semibold">{automation.status.queued_count}</div></div>
            <div className="bg-card p-3"><span className="eyebrow">CAPTCHAs today</span><div className="mt-1 font-semibold">{automation.status.captcha_count}</div></div>
            <div className="bg-card p-3"><span className="eyebrow">Next scan</span><div className="mt-1 font-semibold">{automation.status.next_run_at ? new Date(automation.status.next_run_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—"}</div></div>
          </div>
        )}

        {automation?.status.log.length ? (
          <div className="log-viewer max-h-44">
            <div className="mb-2 flex items-center gap-2 text-muted-foreground">
              <Terminal className="h-3.5 w-3.5" /> Autonomous log
            </div>
            {automation.status.log.slice(-20).map((line, index) => <LogLine key={`${line}-${index}`} line={line} />)}
          </div>
        ) : null}
        <p className="mt-3 text-xs text-muted-foreground">
          Stop prevents new jobs from starting and lets the active application finish. Every pending Qualified job at or above the configured fit score is eligible for autonomous application.
        </p>
        </div>
      </div>

      <div className="card mb-5 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="eyebrow">Manual discovery</p>
          <h3 className="mt-1 section-title">Bring in a specific lead or run a one-off search</h3>
          <p className="mt-1 text-xs text-muted-foreground">These tools do not change the autonomous schedule.</p>
        </div>
        <div className="flex flex-wrap gap-2">
        <button
          onClick={() => {
            setShowLinkedInImport(!showLinkedInImport);
            setShowManualCollector(false);
          }}
          className="btn-secondary"
        >
          <Link2 className="h-4 w-4" /> Import LinkedIn URL
        </button>
        <button
          type="button"
          onClick={() => {
            setShowManualCollector((current) => !current);
            setShowLinkedInImport(false);
          }}
          className="btn-secondary"
        >
          <Search className="h-4 w-4" /> {showManualCollector ? "Close manual scan" : "Run manual scan"}
        </button>
        </div>
      </div>

      {/* LinkedIn URL import */}
      {showLinkedInImport && (
        <div className="card mb-5">
          <h3 className="section-title mb-1">Import LinkedIn job</h3>
          <p className="text-[13px] text-muted-foreground mb-3">
            Paste a LinkedIn listing URL. Hunter will fetch its details and classify it.
          </p>
          <form
            onSubmit={handleLinkedInImport}
            className="flex flex-col sm:flex-row gap-3"
          >
            <label className="sr-only" htmlFor="linkedin-job-url">
              LinkedIn job URL
            </label>
            <input
              id="linkedin-job-url"
              type="url"
              value={linkedInUrl}
              onChange={(e) => setLinkedInUrl(e.target.value)}
              placeholder="https://www.linkedin.com/jobs/view/..."
              className="input-base flex-1"
              required
            />
            <div className="flex gap-2">
              <button
                type="submit"
                disabled={importingLinkedIn || !linkedInUrl.trim()}
                className="btn-primary"
              >
                {importingLinkedIn ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Plus className="w-4 h-4" />
                )}
                {importingLinkedIn ? "Importing…" : "Import job"}
              </button>
              <button
                type="button"
                onClick={() => setShowLinkedInImport(false)}
                className="btn-secondary"
              >
                Cancel
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Job Collector Panel */}
      {showManualCollector && (
      <div className="card mb-5">
        <h3 className="section-title mb-3">{t("collector.title")}</h3>
        <p className="text-[13px] text-muted-foreground mb-4">
          {t("collector.description")}
        </p>
        {/* Source selector */}
        {availableSources.length > 1 && (
          <div className="mb-4">
            <label className="block text-sm font-semibold text-foreground mb-1.5">
              {t("collector.sourcePlatform")}
            </label>
            <div className="flex gap-2 flex-wrap">
              {availableSources.map((source) => (
                <button
                  key={source.name}
                  onClick={() => {
                    setCollectSource(source.name);
                    if (
                      source.name === "indeed" &&
                      collectHoursOld !== "" &&
                      collectHoursOld < 1
                    ) {
                      setCollectHoursOld(1);
                    }
                    if (
                      source.name === "indeed" &&
                      !INDEED_JOB_TYPES.has(collectJobType)
                    ) {
                      setCollectJobType("");
                    }
                  }}
                  disabled={collectionBusy}
                  className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors ${
                    collectSource === source.name
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border bg-card text-foreground hover:border-primary/50"
                  }`}
                >
                  {source.display_name}
                </button>
              ))}
            </div>
          </div>
        )}
        {collectSource === "linkedin" && (
          <label className="block text-sm font-semibold text-foreground mb-4">
            LinkedIn search URL <span className="font-normal text-muted-foreground">(optional)</span>
            <input
              type="url"
              value={linkedInSearchUrl}
              onChange={(e) => setLinkedInSearchUrl(e.target.value)}
              placeholder="https://www.linkedin.com/jobs/search-results/?..."
              disabled={collectionBusy}
              className="input-base !py-2 mt-1.5 w-full"
            />
            <span className="block mt-1 text-xs font-normal text-muted-foreground">
              Uses your saved LinkedIn session in headless mode and preserves the URL&apos;s filters and personalized order. When set, the controls below are ignored.
            </span>
          </label>
        )}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mb-2">
          <label className="text-xs font-medium text-muted-foreground">
            Location
            <input
              value={collectLocation}
              onChange={(e) => setCollectLocation(e.target.value)}
              placeholder="Profile locations"
              disabled={collectionBusy || usingLinkedInSearchUrl}
              className="input-base !py-2 mt-1"
            />
          </label>
          <label className="text-xs font-medium text-muted-foreground">
            Hours old
            <input
              type="number"
              aria-label="Hours old"
              value={collectHoursOld}
              onChange={(e) =>
                setCollectHoursOld(e.target.value ? Number(e.target.value) : "")
              }
              min={collectSource === "linkedin" ? 0.25 : 1}
              step={collectSource === "linkedin" ? 0.25 : 1}
              placeholder="168"
              disabled={collectionBusy || usingLinkedInSearchUrl || indeedWithoutFreshness}
              className="input-base !py-2 mt-1 disabled:opacity-50"
            />
            {collectSource === "linkedin" && (
              <span className="block mt-1 font-normal">0.25 = 15 minutes</span>
            )}
          </label>
          <label className="text-xs font-medium text-muted-foreground">
            Job type
            <select
              value={collectJobType}
              onChange={(e) => setCollectJobType(e.target.value)}
              disabled={collectionBusy || usingLinkedInSearchUrl}
              className="input-base !py-2 mt-1"
            >
              {availableJobTypes.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 self-end h-[38px] px-3 border border-border rounded-xl text-sm font-medium text-foreground">
            <input
              type="checkbox"
              checked={collectIsRemote}
              onChange={(e) => setCollectIsRemote(e.target.checked)}
              disabled={collectionBusy || usingLinkedInSearchUrl}
              className="accent-primary"
            />
            Remote
          </label>
        </div>
        {indeedWithoutFreshness && (
          <p className="text-xs text-muted-foreground mb-4">
            Indeed cannot combine Hours old with Job type or Remote, so freshness is not applied.
          </p>
        )}
        {!indeedWithoutFreshness && <div className="mb-4" />}
        <div className="flex gap-3 mb-4">
          <input
            value={collectTitle}
            onChange={(e) => setCollectTitle(e.target.value)}
            placeholder={t("collector.jobTitlePlaceholder")}
            className="input-base flex-1"
            disabled={collectionBusy || usingLinkedInSearchUrl}
          />
          <input
            type="number"
            value={collectMaxJobs}
            onChange={(e) =>
              setCollectMaxJobs(e.target.value ? Number(e.target.value) : "")
            }
            placeholder={t("collector.maxJobsPlaceholder")}
            min={1}
            max={500}
            className="input-base !w-32 !flex-initial"
            disabled={collectionBusy}
            title={t("collector.maxJobsTitle")}
          />
          {startingCollection ? (
            <button disabled className="btn-dark opacity-70">
              <Loader2 className="w-4 h-4 animate-spin" /> Starting…
            </button>
          ) : collecting ? (
            <button
              onClick={handleStopCollect}
              disabled={stoppingCollection}
              className="btn-destructive"
            >
              {stoppingCollection ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Square className="w-4 h-4" />
              )}
              {stoppingCollection ? "Stopping…" : t("collector.stop")}
            </button>
          ) : classifying ? (
            <button
              onClick={handleStopClassification}
              disabled={stoppingClassification}
              className="btn-destructive"
            >
              {stoppingClassification ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Square className="w-4 h-4" />
              )}
              {stoppingClassification ? "Stopping classification…" : "Stop classification"}
            </button>
          ) : (
            <button onClick={confirmStartCollect} className="btn-dark">
              <Play className="w-4 h-4" /> {t("collector.start")}
            </button>
          )}
        </div>
        {/* Progress bar */}
        {collecting && collectMaxJobs && (
          <div className="mb-4">
            <ProgressBar
              percent={
                collectMaxJobs > 0
                  ? (collected / Number(collectMaxJobs)) * 100
                  : 0
              }
              label={t("collector.collectedProgress", {
                collected,
                max: collectMaxJobs,
              })}
            />
          </div>
        )}
        {classifying && classificationTotal > 0 && (
          <div className="mb-4">
            <ProgressBar
              percent={
                ((classificationCompleted + classificationFailed) /
                  classificationTotal) *
                100
              }
              label={`Classified ${classificationCompleted} of ${classificationTotal}${
                classificationFailed ? ` · ${classificationFailed} failed` : ""
              }`}
            />
          </div>
        )}
        {/* Log output */}
        {collectLog.length > 0 && (
          <div ref={logRef} className="log-viewer">
            <div className="flex items-center gap-2 mb-2 text-muted-foreground">
              <Terminal className="w-3.5 h-3.5" /> {t("collector.collectionLog")}
              {collectionBusy && (
                <Loader2 className="w-3.5 h-3.5 animate-spin text-green-400" />
              )}
            </div>
            {collectLog.map((line, i) => (
              <LogLine key={i} line={line} />
            ))}
          </div>
        )}
      </div>
      )}

    </div>
  );
}
