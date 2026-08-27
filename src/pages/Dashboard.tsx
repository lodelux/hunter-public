import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  Check,
  ChevronRight,
  Clock3,
  Cpu,
  Download,
  FileText,
  Loader2,
  Play,
  Search,
  Settings2,
  ShieldAlert,
  Square,
  UserRound,
  XCircle,
} from "lucide-react";
import {
  checkHealth,
  getAutomation,
  getChromiumStatus,
  getDashboardData,
  getSetupStatus,
  getSystemStatus,
  startAutomation,
  stopAutomation,
  type SetupStatus,
} from "../lib/api";
import type { AutomationResponse, GmailOutcomeStatus, JobCategory, SystemStatusResponse } from "../lib/types";
import { JOB_CATEGORY_OPTIONS } from "../lib/types";
import { LoadingSpinner, PageHeader, ProgressBar } from "../components/ui";
import LogLine from "../components/LogLine";

const EMPTY_COUNTS = Object.fromEntries(
  JOB_CATEGORY_OPTIONS.map(({ value }) => [value, 0]),
) as Record<JobCategory, number>;

const PHASES = [
  { id: "collecting", label: "Discover", icon: Search },
  { id: "classifying", label: "Qualify", icon: Check },
  { id: "applying", label: "Apply", icon: Bot },
  { id: "waiting", label: "Wait", icon: Clock3 },
] as const;

function formatTime(value: string | null | undefined) {
  if (!value) return "Not scheduled";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not scheduled";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function RunnerPhaseRail({ phase }: { phase: string }) {
  return (
    <div className="grid grid-cols-4 gap-2" aria-label={`Runner phase: ${phase}`}>
      {PHASES.map(({ id, label, icon: Icon }) => {
        const active = id === phase || (phase === "idle" && id === "waiting");
        return (
          <div
            key={id}
            className={`rounded-lg border px-3 py-3 transition-colors ${
              active ? "border-primary/35 bg-primary/10 text-primary" : "border-border bg-card text-muted-foreground"
            }`}
          >
            <Icon className="mb-2 h-4 w-4" />
            <p className="text-xs font-semibold">{label}</p>
          </div>
        );
      })}
    </div>
  );
}

function formatBytes(bytes: number) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const unitIndex = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / (1024 ** unitIndex);
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)} ${units[unitIndex]}`;
}

function MachineMetric({
  label,
  value,
  detail,
  percent,
  watchAt = 85,
  criticalAt = 95,
}: {
  label: string;
  value: string;
  detail: string;
  percent: number | null;
  watchAt?: number;
  criticalAt?: number;
}) {
  const fillTone = percent !== null && percent >= criticalAt
    ? "bg-destructive"
    : percent !== null && percent >= watchAt
      ? "bg-warning"
      : "bg-foreground/35";

  return (
    <div className="py-3 first:pt-0 last:pb-0">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-foreground">{label}</p>
          <p className="truncate text-xs text-muted-foreground" title={detail}>{detail}</p>
        </div>
        <span className="shrink-0 text-sm font-semibold tabular-nums text-foreground">{value}</span>
      </div>
      {percent !== null && (
        <div className="h-1.5 overflow-hidden rounded-full bg-secondary" aria-hidden="true">
          <div
            className={`h-full rounded-full transition-[width,background-color] duration-300 ${fillTone}`}
            style={{ width: `${Math.max(0, Math.min(100, percent))}%` }}
          />
        </div>
      )}
    </div>
  );
}

function StorageMetric({ disk }: { disk: SystemStatusResponse["disk"] }) {
  const usedPercent = Math.max(0, Math.min(100, disk.percent));
  const measuredHunterBytes = Number.isFinite(disk.hunter_data_bytes) ? disk.hunter_data_bytes : 0;
  const hunterBytes = Math.max(0, Math.min(measuredHunterBytes, disk.used_bytes));
  const hunterShareOfUsed = disk.used_bytes > 0 ? (hunterBytes / disk.used_bytes) * 100 : 0;
  const otherTone = disk.percent >= 95
    ? "bg-destructive"
    : disk.percent >= 85
      ? "bg-warning"
      : "bg-foreground/35";

  return (
    <div className="py-3 first:pt-0 last:pb-0">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-foreground">Storage</p>
          <p className="truncate text-xs text-muted-foreground">
            {formatBytes(disk.free_bytes)} free · {formatBytes(disk.used_bytes)} used
          </p>
        </div>
        <span className="shrink-0 text-sm font-semibold tabular-nums text-foreground">
          {formatBytes(disk.total_bytes)} total
        </span>
      </div>
      <div
        className="h-1.5 overflow-hidden rounded-full bg-secondary"
        role="img"
        aria-label={`${formatBytes(disk.used_bytes)} of ${formatBytes(disk.total_bytes)} used; Hunter data uses ${formatBytes(hunterBytes)}`}
      >
        <div className="flex h-full transition-[width] duration-300" style={{ width: `${usedPercent}%` }}>
          {hunterBytes > 0 && (
            <div className="h-full shrink-0 bg-primary" style={{ width: `${hunterShareOfUsed}%` }} />
          )}
          <div className={`h-full flex-1 ${otherTone}`} />
        </div>
      </div>
      <div className="mt-2 flex items-center justify-between gap-3 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-1.5 w-1.5 rounded-full bg-primary" aria-hidden="true" />
          Hunter {formatBytes(hunterBytes)}
        </span>
        <span className="tabular-nums">{Math.round(disk.percent)}% used</span>
      </div>
    </div>
  );
}

function MachineStatusCard({
  status,
  error,
}: {
  status: SystemStatusResponse | null;
  error: boolean;
}) {
  const state = status?.headroom.state || "healthy";
  const stateLabel = error
    ? status ? "Stale" : "Unavailable"
    : status
    ? state === "critical"
      ? "Under pressure"
      : state === "watch"
        ? "Watch"
        : "Ready"
    : "Reading";
  const stateTone = error
    ? "bg-muted-foreground/50"
    : status
    ? state === "critical"
      ? "bg-destructive"
      : state === "watch"
        ? "bg-warning"
        : "bg-success"
    : "bg-muted-foreground/50";

  return (
    <section className="card" aria-labelledby="machine-status-heading">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="eyebrow">Machine status</p>
          <h3 id="machine-status-heading" className="mt-1 text-lg font-semibold tracking-[-0.02em]">
            Worker headroom
          </h3>
        </div>
        <span className="status-chip shrink-0">
          <span className={`h-2 w-2 rounded-full ${stateTone}`} />
          {stateLabel}
        </span>
      </div>

      {!status ? (
        <p className="mt-4 text-sm leading-6 text-muted-foreground">
          {error ? "Machine telemetry is unavailable. Hunter will keep retrying." : "Reading current resource pressure…"}
        </p>
      ) : (
        <>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">
            {error ? "The latest reading could not be refreshed; showing the last snapshot." : status.headroom.summary}
          </p>
          <div className="mt-4 divide-y divide-border">
            <MachineMetric
              label="CPU"
              value={`${Math.round(status.cpu.percent)}%`}
              detail="Current system load"
              percent={status.cpu.percent}
            />
            <MachineMetric
              label="Memory"
              value={`${Math.round(status.memory.percent)}%`}
              detail={`${formatBytes(status.memory.used_bytes)} of ${formatBytes(status.memory.total_bytes)} used`}
              percent={status.memory.percent}
            />
            <MachineMetric
              label="Swap"
              value={status.swap.total_bytes > 0 ? `${Math.round(status.swap.percent)}%` : "None"}
              detail={status.swap.total_bytes > 0
                ? `${formatBytes(status.swap.used_bytes)} of ${formatBytes(status.swap.total_bytes)} used`
                : "No swap space configured"}
              percent={status.swap.total_bytes > 0 ? status.swap.percent : null}
              watchAt={50}
              criticalAt={85}
            />
            <MachineMetric
              label="Temperature"
              value={status.temperature.available && status.temperature.celsius !== null
                ? `${Math.round(status.temperature.celsius)}°C`
                : "Unavailable"}
              detail={status.temperature.sensor || "No readable sensor"}
              percent={status.temperature.available ? status.temperature.celsius : null}
              watchAt={Math.min(status.temperature.high_celsius ?? 80, 80)}
              criticalAt={Math.min(status.temperature.critical_celsius ?? 95, 95)}
            />
            <StorageMetric disk={status.disk} />
          </div>
          <p className="mt-4 text-[11px] text-muted-foreground">
            Updated {new Date(status.collected_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
          </p>
        </>
      )}
    </section>
  );
}

export default function Dashboard() {
  const { t } = useTranslation("dashboard");
  const navigate = useNavigate();
  const [stats, setStats] = useState({
    totalJobs: 0,
    applied: 0,
    failed: 0,
    availableToApply: 0,
    successRate: 0,
    totalMemories: 0,
    categoryCounts: EMPTY_COUNTS,
  });
  const [automation, setAutomation] = useState<AutomationResponse | null>(null);
  const [automationBusy, setAutomationBusy] = useState(false);
  const [automationError, setAutomationError] = useState<string | null>(null);
  const [backendOk, setBackendOk] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [setupStatus, setSetupStatus] = useState<SetupStatus | null>(null);
  const [chromiumState, setChromiumState] = useState<{ state: string; message: string } | null>(null);
  const [gmailOutcomes, setGmailOutcomes] = useState<GmailOutcomeStatus | null>(null);
  const [machineStatus, setMachineStatus] = useState<SystemStatusResponse | null>(null);
  const [machineStatusError, setMachineStatusError] = useState(false);

  const loadDashboard = useCallback(async () => {
    const [data, setup] = await Promise.all([
      getDashboardData(),
      getSetupStatus().catch(() => null),
    ]);
    const jobs = data.jobs || {
      total: 0,
      applied: 0,
      failed: 0,
      pending: 0,
      blocked: 0,
      in_progress: 0,
      category_counts: EMPTY_COUNTS,
    };
    const mem = data.memory || { total_memories: 0, unique_domains: 0, by_category: {} };
    const applied = jobs.applied || 0;
    const failed = jobs.failed || 0;
    const attempts = applied + failed;
    setStats({
      totalJobs: jobs.total || 0,
      applied,
      failed,
      availableToApply: jobs.qualified || 0,
      successRate: attempts > 0 ? Math.round((applied / attempts) * 100) : 0,
      totalMemories: mem.total_memories || 0,
      categoryCounts: { ...EMPTY_COUNTS, ...(jobs.category_counts || {}) },
    });
    setGmailOutcomes(data.gmail_outcomes || null);
    if (setup) setSetupStatus(setup);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let lastHealth: boolean | null = null;
    let healthTimer: number | undefined;

    async function init() {
      try {
        for (let attempt = 0; attempt < 10; attempt += 1) {
          try {
            await checkHealth();
            lastHealth = true;
            if (!cancelled) setBackendOk(true);
            await loadDashboard().catch(() => {});
            return;
          } catch {
            if (attempt === 9) {
              lastHealth = false;
              if (!cancelled) setBackendOk(false);
              return;
            }
            await new Promise((resolve) => setTimeout(resolve, 1500));
          }
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    const healthPoll = async () => {
      let nextDelay = document.hidden ? 60000 : 30000;
      try {
        if (!document.hidden) {
          await checkHealth();
          const recovered = lastHealth === false;
          lastHealth = true;
          if (!cancelled) setBackendOk(true);
          if (recovered) await loadDashboard().catch(() => {});
        }
      } catch {
        lastHealth = false;
        nextDelay = 10000;
        if (!cancelled) setBackendOk(false);
      } finally {
        if (!cancelled) healthTimer = window.setTimeout(healthPoll, nextDelay);
      }
    };

    void init().finally(() => {
      if (!cancelled) healthTimer = window.setTimeout(healthPoll, 30000);
    });
    return () => {
      cancelled = true;
      if (healthTimer !== undefined) window.clearTimeout(healthTimer);
    };
  }, [loadDashboard]);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;

    const refresh = async () => {
      const delay = document.hidden ? 60000 : 10000;
      if (!document.hidden) {
        try {
          const response = await getSystemStatus();
          if (active) {
            setMachineStatus(response);
            setMachineStatusError(false);
          }
        } catch {
          if (active) setMachineStatusError(true);
        }
      }
      if (active) timer = window.setTimeout(refresh, delay);
    };

    void refresh();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    let previousCycle: string | null | undefined;

    const refresh = async () => {
      let delay = 10000;
      try {
        if (document.hidden) return;
        const response = await getAutomation();
        if (!active) return;
        setAutomation(response);
        if (
          previousCycle !== undefined
          && response.status.last_cycle_finished_at
          && response.status.last_cycle_finished_at !== previousCycle
        ) {
          void loadDashboard();
        }
        previousCycle = response.status.last_cycle_finished_at;
        delay = response.status.state === "running" ? 2000 : 10000;
      } catch {
        delay = 5000;
      } finally {
        if (active) timer = window.setTimeout(refresh, delay);
      }
    };

    void refresh();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [loadDashboard]);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const poll = () => {
      getChromiumStatus()
        .then((status) => {
          if (!active) return;
          setChromiumState(status);
          if (status.state === "installing" || status.state === "checking") {
            timer = window.setTimeout(poll, 2000);
          }
        })
        .catch(() => {
          if (active) timer = window.setTimeout(poll, 3000);
        });
    };
    poll();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    let active = true;
    const refreshSetup = () => {
      getSetupStatus()
        .then((status) => {
          if (active) setSetupStatus(status);
        })
        .catch(() => {});
    };
    window.addEventListener("focus", refreshSetup);
    window.addEventListener("langhire:setup-updated", refreshSetup);
    document.addEventListener("visibilitychange", refreshSetup);
    return () => {
      active = false;
      window.removeEventListener("focus", refreshSetup);
      window.removeEventListener("langhire:setup-updated", refreshSetup);
      document.removeEventListener("visibilitychange", refreshSetup);
    };
  }, []);

  const handleAutomationToggle = async () => {
    setAutomationBusy(true);
    setAutomationError(null);
    try {
      const response = automation?.status.state === "running"
        ? await stopAutomation()
        : await startAutomation();
      setAutomation(response);
    } catch (error) {
      setAutomationError(error instanceof Error ? error.message : "Could not update the runner");
    } finally {
      setAutomationBusy(false);
    }
  };

  const setupSteps = [
    { label: t("setup.configureAI"), done: setupStatus?.llm, path: "/llm", icon: Cpu },
    { label: t("setup.setResume"), done: setupStatus?.resume, path: "/settings", icon: FileText },
    { label: t("setup.setupProfile"), done: setupStatus?.profile, path: "/profile", icon: UserRound },
  ];
  const completedSetup = setupSteps.filter((step) => step.done).length;
  const automationState = automation?.status.state || "stopped";
  const automationRunning = automationState === "running";
  const automationPaused = automationState === "paused";
  const statusTitle = automationRunning
    ? automation?.status.phase === "waiting"
      ? "Runner is watching for the next cycle"
      : `Runner is ${automation?.status.phase || "working"}`
    : automationPaused
      ? "Runner needs your attention"
      : "Runner is ready when you are";
  const statusCopy = automationRunning
    ? automation?.status.current_source
      ? `Working through ${automation.status.current_source} · next scan ${formatTime(automation.status.next_run_at)}`
      : `Next scan ${formatTime(automation?.status.next_run_at)}`
    : automationPaused
      ? automation?.status.pause_reason || "Review the pause reason before resuming."
      : "Start a cycle to discover, qualify, and apply without babysitting the queue.";

  const attentionItems = useMemo(() => {
    const items = [
      {
        label: "Unreviewed jobs",
        description: "Waiting for a human decision",
        count: stats.categoryCounts.review,
        path: "/jobs?tab=pending&category=review",
        tone: "text-warning",
      },
      {
        label: "Failed attempts",
        description: "Safe to inspect or retry manually",
        count: stats.categoryCounts.failed || stats.failed,
        path: "/jobs?tab=pending&category=failed",
        tone: "text-destructive",
      },
    ];
    if (gmailOutcomes?.review_required_count) {
      items.push({
        label: "Email outcomes",
        description: "Matched Gmail messages need a decision",
        count: gmailOutcomes.review_required_count,
        path: "/jobs?tab=pending&category=rejected&review=email",
        tone: "text-warning",
      });
    } else if (gmailOutcomes?.last_error) {
      items.push({
        label: "Gmail sync",
        description: "Outcome synchronization needs attention",
        count: 1,
        path: "/settings?section=gmail",
        tone: "text-destructive",
      });
    }
    return items;
  }, [gmailOutcomes, stats]);
  const hasAttention = attentionItems.some((item) => item.count > 0);

  if (loading) {
    return (
      <div>
        <PageHeader title="Today" subtitle="Your autonomous job-search workspace" />
        {chromiumState?.state === "installing" && (
          <div className="card mb-5 flex items-center gap-3">
            <Download className="h-5 w-5 text-primary" />
            <div className="flex-1">
              <p className="text-sm font-semibold">{t("chromium.installing")}</p>
              <p className="mt-0.5 text-xs text-muted-foreground">{chromiumState.message}</p>
            </div>
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        )}
        <LoadingSpinner />
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title="Today"
        subtitle="See what Hunter is doing, what needs you, and what moved forward."
        actions={
          <button type="button" onClick={() => navigate("/jobs")} className="btn-secondary">
            <Settings2 className="h-4 w-4" /> Configure runner
          </button>
        }
      />

      {backendOk === false && (
        <div className="error-banner mb-5 flex items-start gap-2">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>Hunter’s backend is not connected. The runner and job data will return automatically when it recovers.</span>
        </div>
      )}
      {chromiumState?.state === "installing" && (
        <div className="card mb-5 flex items-center gap-3">
          <Download className="h-5 w-5 text-primary" />
          <div className="flex-1">
            <p className="text-sm font-semibold">{t("chromium.installing")}</p>
            <p className="mt-0.5 text-xs text-muted-foreground">{chromiumState.message}</p>
          </div>
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      )}
      {chromiumState?.state === "failed" && (
        <div className="error-banner mb-5">Chromium install failed: {chromiumState.message}</div>
      )}

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.65fr)_minmax(310px,0.75fr)]">
        <section className="card flex min-h-0 flex-col overflow-hidden !p-0" aria-labelledby="runner-heading">
          <div className="border-b border-border p-5 sm:p-6">
            <div className="flex flex-col gap-5 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0">
                <div className="mb-3 flex items-center gap-2">
                  <span className={`h-2.5 w-2.5 rounded-full ${
                    automationRunning ? "bg-success" : automationPaused ? "bg-warning" : "bg-muted-foreground/50"
                  }`} />
                  <span className="eyebrow">Autonomous runner · {automationState}</span>
                </div>
                <h3 id="runner-heading" className="text-xl font-semibold tracking-[-0.025em] text-foreground">
                  {statusTitle}
                </h3>
                <p className="mt-1.5 max-w-2xl text-sm leading-6 text-muted-foreground">{statusCopy}</p>
              </div>
              <button
                type="button"
                onClick={() => void handleAutomationToggle()}
                disabled={automationBusy || !automation}
                className={automationRunning ? "btn-secondary shrink-0" : "btn-primary shrink-0"}
              >
                {automationBusy
                  ? <Loader2 className="h-4 w-4 animate-spin" />
                  : automationRunning
                    ? <Square className="h-4 w-4" />
                    : automationPaused
                      ? <Play className="h-4 w-4" />
                      : <Play className="h-4 w-4" />}
                {automationRunning ? "Stop after current job" : automationPaused ? "Resume runner" : "Start runner"}
              </button>
            </div>

            {automationPaused && automation?.status.pause_reason && (
              <div className="mt-5 flex gap-3 rounded-lg border border-warning/30 bg-warning/10 p-4 text-sm text-foreground">
                <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                <div>
                  <p className="font-semibold">Manual decision required</p>
                  <p className="mt-0.5 text-muted-foreground">{automation.status.pause_reason}</p>
                </div>
              </div>
            )}
            {automationError && <div className="error-banner mt-5">{automationError}</div>}

            <div className="mt-6">
              <RunnerPhaseRail phase={automation?.status.phase || "idle"} />
            </div>
          </div>

          <div className="grid gap-px bg-border sm:grid-cols-4">
            {[
              ["Started today", `${automation?.status.today_started || 0} / ${automation?.status.today_limit || automation?.config.daily_job_limit || 0}`],
              ["Waiting in queue", automation?.status.queued_count || 0],
              ["Next scan", formatTime(automation?.status.next_run_at)],
              ["Minimum fit", `${automation?.config.automation_min_score ?? 70}+`],
            ].map(([label, value]) => (
              <div key={label} className="bg-card px-5 py-4">
                <p className="eyebrow">{label}</p>
                <p className="mt-1 text-lg font-semibold tracking-[-0.02em] text-foreground tabular-nums">{value}</p>
              </div>
            ))}
          </div>

          {automation?.status.log.length ? (
            <div className="flex min-h-40 flex-1 flex-col border-t border-border p-5">
              <div className="mb-3 flex items-center justify-between">
                <p className="eyebrow">Recent runner activity</p>
                <button type="button" onClick={() => navigate("/logs")} className="text-xs font-semibold text-primary hover:underline">
                  All activity
                </button>
              </div>
              <div className="log-viewer min-h-0 flex-1 max-h-none">
                {automation.status.log.slice(-8).map((line, index) => (
                  <LogLine key={`${line}-${index}`} line={line} />
                ))}
              </div>
            </div>
          ) : null}
        </section>

        <div className="space-y-5">
          <section className="card" aria-labelledby="attention-heading">
            <div className="mb-4 flex items-center justify-between">
              <div>
                <p className="eyebrow">Human queue</p>
                <h3 id="attention-heading" className="mt-1 text-lg font-semibold tracking-[-0.02em]">Needs you</h3>
              </div>
              {hasAttention
                ? <AlertTriangle className="h-5 w-5 text-warning" />
                : <Check className="h-5 w-5 text-success" />}
            </div>
            <div className="divide-y divide-border">
              {attentionItems.map((item) => (
                <button
                  key={item.label}
                  type="button"
                  onClick={() => navigate(item.path)}
                  className="group flex w-full items-center gap-3 py-3 text-left first:pt-0 last:pb-0"
                >
                  <span className={`w-8 text-xl font-semibold tabular-nums ${item.tone}`}>{item.count}</span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-semibold text-foreground">{item.label}</span>
                    <span className="block truncate text-xs text-muted-foreground">{item.description}</span>
                  </span>
                  <ChevronRight className="h-4 w-4 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-foreground" />
                </button>
              ))}
            </div>
          </section>

          <MachineStatusCard status={machineStatus} error={machineStatusError} />

          {!setupStatus?.all_required_done && (
            <section className="card" aria-labelledby="setup-heading">
              <p className="eyebrow">Before unattended runs</p>
              <div className="mt-1 flex items-center justify-between">
                <h3 id="setup-heading" className="text-lg font-semibold tracking-[-0.02em]">{t("setup.gettingStarted")}</h3>
                <span className="text-xs font-semibold text-muted-foreground">{completedSetup}/{setupSteps.length}</span>
              </div>
              <div className="my-4">
                <ProgressBar percent={(completedSetup / setupSteps.length) * 100} showPercent={false} />
              </div>
              <div className="space-y-1">
                {setupSteps.map(({ label, done, path, icon: Icon }) => (
                  <button
                    key={label}
                    type="button"
                    onClick={() => !done && navigate(path)}
                    className="flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left hover:bg-secondary"
                  >
                    <span className={`flex h-6 w-6 items-center justify-center rounded-full ${done ? "bg-success/15 text-success" : "bg-secondary text-muted-foreground"}`}>
                      {done ? <Check className="h-3.5 w-3.5" /> : <Icon className="h-3.5 w-3.5" />}
                    </span>
                    <span className={`flex-1 text-sm ${done ? "text-muted-foreground line-through" : "font-medium text-foreground"}`}>{label}</span>
                    {!done && <ArrowRight className="h-3.5 w-3.5 text-muted-foreground" />}
                  </button>
                ))}
              </div>
            </section>
          )}
        </div>
      </div>

      <section className="card mt-5 !p-0" aria-label="Search pipeline">
        <div className="border-b border-border px-5 py-4">
          <p className="eyebrow">Search pipeline</p>
        </div>
        <div className="grid gap-px bg-border sm:grid-cols-2 xl:grid-cols-5">
          {[
            ["Discovered", stats.totalJobs, "All collected listings"],
            ["Qualified", stats.categoryCounts.qualified, "Passed deterministic screening"],
            ["Pending", stats.availableToApply, "Available to apply"],
            ["Applied", stats.applied, `${stats.successRate}% verified attempt success`],
            [
              "Next steps",
              stats.categoryCounts.online_assessment + stats.categoryCounts.interview + stats.categoryCounts.offer,
              "Assessments, interviews, and offers",
            ],
          ].map(([label, value, helper]) => (
            <div key={label} className="bg-card px-5 py-4">
              <p className="metric-value">{value}</p>
              <p className="mt-1 text-sm font-semibold text-foreground">{label}</p>
              <p className="mt-0.5 text-xs text-muted-foreground">{helper}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
