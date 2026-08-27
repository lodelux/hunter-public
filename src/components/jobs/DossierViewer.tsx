import { useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  Download,
  ExternalLink,
  FileText,
  Film,
  Image as ImageIcon,
  Loader2,
  MoveRight,
  Target,
  RotateCcw,
  ShieldCheck,
  X,
  XCircle,
} from "lucide-react";
import {
  getApplicationAttempt,
  getApplicationAttemptFile,
} from "../../lib/api";
import type {
  ApplicationAttemptDossier,
  ApplicationAttemptManifest,
  ApplicationOutcomeDecision,
  GenerationAuditDecision,
  GenerationAuditReference,
} from "../../lib/types";

interface DossierViewerProps {
  attemptId: string;
  outcomeResolutionRequired?: boolean;
  onResolveOutcome?: (decision: ApplicationOutcomeDecision) => Promise<void>;
  onClose: () => void;
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function formatDuration(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function formatSize(bytes: number | null): string {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTokens(value: number): string {
  return value.toLocaleString();
}

function signalDetails(value: boolean | null, positive: string, negative: string) {
  if (value === true) {
    return {
      label: positive,
      icon: CheckCircle2,
      className: "bg-success/10 text-success",
    };
  }
  if (value === false) {
    return {
      label: negative,
      icon: XCircle,
      className: "bg-destructive/10 text-destructive",
    };
  }
  return {
    label: "Not available",
    icon: CircleHelp,
    className: "bg-secondary text-muted-foreground",
  };
}

function judgementText(manifest: ApplicationAttemptManifest): string | null {
  const judgement = manifest.judgement;
  if (!judgement) return null;
  for (const key of ["reasoning", "failure_reason", "reason"]) {
    if (typeof judgement[key] === "string") return judgement[key] as string;
  }
  return JSON.stringify(judgement, null, 2);
}

function useAttemptAsset(attemptId: string, path: string | null) {
  const assetKey = `${attemptId}:${path || ""}`;
  const [asset, setAsset] = useState({
    key: assetKey,
    url: null as string | null,
    loading: Boolean(path),
    error: false,
  });
  const current = asset.key === assetKey
    ? asset
    : { key: assetKey, url: null, loading: Boolean(path), error: false };

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    if (!path) return;
    getApplicationAttemptFile(attemptId, path)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setAsset({ key: assetKey, url: objectUrl, loading: false, error: false });
      })
      .catch(() => {
        if (!cancelled) {
          setAsset({ key: assetKey, url: null, loading: false, error: true });
        }
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [assetKey, attemptId, path]);

  return current;
}

function Signal({
  title,
  value,
  positive,
  negative,
}: {
  title: string;
  value: boolean | null;
  positive: string;
  negative: string;
}) {
  const signal = signalDetails(value, positive, negative);
  const Icon = signal.icon;
  return (
    <div className="min-w-0 border-r border-border px-4 py-3 last:border-r-0">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">{title}</p>
      <div className={`mt-1.5 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${signal.className}`}>
        <Icon className="h-3.5 w-3.5" />
        {signal.label}
      </div>
    </div>
  );
}

function ReferenceList({
  references,
  kind,
}: {
  references: GenerationAuditReference[];
  kind: "requirement" | "evidence";
}) {
  if (references.length === 0) {
    return <p className="text-xs text-muted-foreground">No citation recorded.</p>;
  }
  return (
    <ul className="space-y-1.5">
      {references.map((reference) => (
        <li
          key={`${kind}-${reference.id}`}
          className={kind === "requirement"
            ? "rounded-md bg-warning/10 px-2.5 py-2 text-xs text-foreground"
            : "rounded-md bg-secondary/70 px-2.5 py-2 text-xs text-foreground"}
        >
          <span className="mr-1.5 font-mono text-[10px] font-semibold text-muted-foreground">
            {reference.id}
          </span>
          {reference.text}
        </li>
      ))}
    </ul>
  );
}

function DecisionTrail({ decision }: { decision: GenerationAuditDecision }) {
  return (
    <div className="rounded-lg border border-border bg-background p-3">
      <p className="text-sm font-semibold text-foreground">{decision.decision}</p>
      <div className="mt-3 grid items-start gap-2 md:grid-cols-[minmax(0,1fr)_20px_minmax(0,1fr)]">
        <div>
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Target need</p>
          <ReferenceList references={decision.requirements} kind="requirement" />
        </div>
        <MoveRight className="mx-auto mt-7 hidden h-4 w-4 text-muted-foreground md:block" />
        <div>
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Candidate evidence</p>
          <ReferenceList references={decision.evidence} kind="evidence" />
        </div>
      </div>
    </div>
  );
}

export default function DossierViewer({
  attemptId,
  outcomeResolutionRequired = false,
  onResolveOutcome,
  onClose,
}: DossierViewerProps) {
  const [dossier, setDossier] = useState<ApplicationAttemptDossier | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [screenshotIndex, setScreenshotIndex] = useState(0);
  const [showRecording, setShowRecording] = useState(false);
  const [downloadingPath, setDownloadingPath] = useState<string | null>(null);
  const [confirmingRetry, setConfirmingRetry] = useState(false);
  const [resolvingOutcome, setResolvingOutcome] = useState<ApplicationOutcomeDecision | null>(null);
  const [resolutionError, setResolutionError] = useState<string | null>(null);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    setDossier(null);
    setError(null);
    setConfirmingRetry(false);
    setResolvingOutcome(null);
    setResolutionError(null);
    getApplicationAttempt(attemptId)
      .then((value) => {
        if (!cancelled) {
          setDossier(value);
          setScreenshotIndex(0);
        }
      })
      .catch((reason) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : "Could not load dossier");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [attemptId]);

  const currentScreenshot = dossier?.screenshots[screenshotIndex] || null;
  const screenshot = useAttemptAsset(attemptId, currentScreenshot);
  const recording = useAttemptAsset(
    attemptId,
    showRecording ? dossier?.recording || null : null,
  );
  const judgeNotes = dossier ? judgementText(dossier.manifest) : null;
  const issues = useMemo(() => {
    if (!dossier) return [];
    return Array.from(new Set([
      ...(dossier.manifest.errors || []),
      dossier.manifest.error,
      dossier.manifest.recording_error,
    ].filter((value): value is string => Boolean(value))));
  }, [dossier]);

  const downloadFile = async (path: string) => {
    setDownloadingPath(path);
    try {
      const blob = await getApplicationAttemptFile(attemptId, path);
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = path.split("/").pop() || "dossier-file";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(objectUrl);
    } catch (reason) {
      alert(reason instanceof Error ? reason.message : "Could not download file");
    } finally {
      setDownloadingPath(null);
    }
  };

  const resolveOutcome = async (decision: ApplicationOutcomeDecision) => {
    if (!onResolveOutcome) return;
    setResolvingOutcome(decision);
    setResolutionError(null);
    try {
      await onResolveOutcome(decision);
    } catch (reason) {
      setResolutionError(
        reason instanceof Error ? reason.message : "Could not resolve the application outcome",
      );
    } finally {
      setResolvingOutcome(null);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/45 p-3 backdrop-blur-sm sm:p-6"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label="Application audit dossier"
        className="mx-auto flex h-full max-w-7xl flex-col overflow-hidden rounded-2xl border border-border bg-background"
      >
        <header className="flex flex-shrink-0 items-center justify-between gap-4 border-b border-border bg-card px-5 py-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              <ShieldCheck className="h-4 w-4 text-primary" /> Application dossier
            </div>
            <h2 className="mt-1 truncate text-xl font-bold text-foreground">
              {dossier?.manifest.job_title || "Loading application evidence…"}
            </h2>
            {dossier && (
              <p className="truncate text-sm text-muted-foreground">{dossier.manifest.company || "Unknown company"}</p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            autoFocus
            aria-label="Close dossier"
            className="rounded-lg p-2 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          >
            <X className="h-5 w-5" />
          </button>
        </header>

        {error ? (
          <div className="flex flex-1 items-center justify-center p-8">
            <div className="max-w-lg rounded-xl border border-destructive/20 bg-destructive/5 p-5 text-center">
              <XCircle className="mx-auto h-8 w-8 text-destructive" />
              <p className="mt-3 font-semibold text-foreground">Dossier unavailable</p>
              <p className="mt-1 text-sm text-muted-foreground">{error}</p>
            </div>
          </div>
        ) : !dossier ? (
          <div className="flex flex-1 items-center justify-center">
            <Loader2 className="h-7 w-7 animate-spin text-primary" />
          </div>
        ) : (
          <div className="flex-1 overflow-y-auto">
            <div className="mx-auto max-w-7xl space-y-5 p-5">
              <p className="rounded-lg bg-warning/10 px-3 py-2 text-xs text-foreground">
                Sensitive audit evidence. Screenshots and recordings may include personal data, Gmail, or OTPs.
              </p>

              <div className="grid overflow-hidden rounded-xl border border-border bg-card sm:grid-cols-4">
                <Signal title="Agent result" value={dossier.manifest.agent_claimed_success} positive="Claimed success" negative="Did not succeed" />
                <Signal title="Independent judge" value={dossier.manifest.judge_verdict} positive="Validated" negative="Not validated" />
                <Signal title="Submission" value={dossier.manifest.submission_confirmed} positive="Confirmed" negative="Not confirmed" />
                <Signal
                  title="LinkedIn outreach"
                  value={dossier.manifest.linkedin_outreach?.status === "sent" ? true : dossier.manifest.linkedin_outreach?.status === "failed" ? false : null}
                  positive="Sent with CV"
                  negative="Not sent"
                />
              </div>

              {dossier.manifest.linkedin_outreach && (
                <section className="rounded-xl border border-border bg-card p-4">
                  <h3 className="text-sm font-bold text-foreground">LinkedIn outreach</h3>
                  <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-3">
                    <div>
                      <dt className="text-xs uppercase text-muted-foreground">Recipient</dt>
                      <dd className="mt-1 text-foreground">{dossier.manifest.linkedin_outreach.recipient || "—"}</dd>
                    </div>
                    <div>
                      <dt className="text-xs uppercase text-muted-foreground">CV attachment</dt>
                      <dd className="mt-1 text-foreground">{dossier.manifest.linkedin_outreach.cv_attached ? "Verified" : "Not verified"}</dd>
                    </div>
                    <div>
                      <dt className="text-xs uppercase text-muted-foreground">Evidence</dt>
                      <dd className="mt-1 text-foreground">
                        {dossier.manifest.linkedin_outreach.evidence || dossier.manifest.linkedin_outreach.reason || "—"}
                      </dd>
                    </div>
                  </dl>
                </section>
              )}

              {dossier.generation_audit && (
                <details className="group overflow-hidden rounded-xl border border-border bg-card">
                  <summary className="flex cursor-pointer list-none items-start gap-3 px-4 py-3">
                    <span className="mt-0.5 rounded-lg bg-warning/10 p-2 text-warning">
                      <Target className="h-4 w-4" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <h3 className="text-sm font-bold text-foreground">Why these documents look this way</h3>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        The target needs, candidate evidence, and positioning choices used before the browser opened.
                      </p>
                    </div>
                    <ChevronRight className="mt-2 h-4 w-4 flex-shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
                  </summary>

                  <div className="grid divide-y divide-border border-t border-border xl:grid-cols-2 xl:divide-x xl:divide-y-0">
                    {dossier.generation_audit.resume && (
                      <div className="min-w-0 p-4">
                        <div className="flex flex-wrap items-start justify-between gap-2">
                          <div>
                            <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Tailored CV</p>
                            <p className="mt-1 text-sm font-semibold text-foreground">
                              {dossier.generation_audit.resume.positioning}
                            </p>
                          </div>
                          <span className="rounded-full bg-secondary px-2 py-1 font-mono text-[10px] text-muted-foreground">
                            {dossier.generation_audit.resume.model} · {dossier.generation_audit.resume.reasoning_effort}
                          </span>
                        </div>

                        <div className="mt-4 space-y-2">
                          {dossier.generation_audit.resume.priorities.map((decision, index) => (
                            <DecisionTrail key={`resume-priority-${index}`} decision={decision} />
                          ))}
                        </div>

                        {dossier.manifest.token_breakdown?.cv && (
                          <p className="mt-3 font-mono text-[10px] text-muted-foreground">
                            {formatTokens(dossier.manifest.token_breakdown.cv.prompt_tokens)} prompt · {formatTokens(dossier.manifest.token_breakdown.cv.cached_prompt_tokens)} cached · {formatTokens(dossier.manifest.token_breakdown.cv.cache_write_tokens)} cache-write · {dossier.manifest.token_breakdown.cv.reasoning_tokens == null ? `${formatTokens(dossier.manifest.token_breakdown.cv.completion_tokens)} aggregate completion` : `${formatTokens(dossier.manifest.token_breakdown.cv.visible_completion_tokens)} visible output · ${formatTokens(dossier.manifest.token_breakdown.cv.reasoning_tokens)} reasoning`} tokens
                          </p>
                        )}

                        <details className="mt-3 rounded-lg border border-border bg-background">
                          <summary className="cursor-pointer list-none px-3 py-2.5 text-xs font-semibold text-foreground">
                            Inspect {dossier.generation_audit.resume.selections.length} selected CV claims
                          </summary>
                          <div className="max-h-96 space-y-2 overflow-y-auto border-t border-border p-3">
                            {dossier.generation_audit.resume.selections.map((selection, index) => (
                              <details key={`selection-${index}`} className="rounded-md bg-secondary/50 px-3 py-2">
                                <summary className="cursor-pointer list-none">
                                  <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                                    {selection.section} · relevance {selection.relevance}
                                  </p>
                                  <p className="mt-1 text-xs font-semibold text-foreground">{selection.text}</p>
                                </summary>
                                <div className="mt-3 grid gap-3 border-t border-border pt-3 sm:grid-cols-2">
                                  <div>
                                    <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Matched need</p>
                                    <ReferenceList references={selection.requirements} kind="requirement" />
                                  </div>
                                  <div>
                                    <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Supporting source</p>
                                    <ReferenceList references={selection.evidence} kind="evidence" />
                                  </div>
                                </div>
                              </details>
                            ))}
                          </div>
                        </details>
                      </div>
                    )}

                    {dossier.generation_audit.cover_letter && (
                      <div className="min-w-0 p-4">
                        <div className="flex flex-wrap items-start justify-between gap-2">
                          <div>
                            <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Cover letter</p>
                            <p className="mt-1 text-sm font-semibold text-foreground">
                              {dossier.generation_audit.cover_letter.positioning}
                            </p>
                          </div>
                          <span className="rounded-full bg-secondary px-2 py-1 font-mono text-[10px] text-muted-foreground">
                            {dossier.generation_audit.cover_letter.model} · {dossier.generation_audit.cover_letter.reasoning_effort}
                          </span>
                        </div>
                        <div className="mt-4 space-y-2">
                          {dossier.generation_audit.cover_letter.decisions.map((decision, index) => (
                            <DecisionTrail key={`letter-decision-${index}`} decision={decision} />
                          ))}
                        </div>
                        {dossier.manifest.token_breakdown?.cover_letter && (
                          <p className="mt-3 font-mono text-[10px] text-muted-foreground">
                            {formatTokens(dossier.manifest.token_breakdown.cover_letter.prompt_tokens)} prompt · {formatTokens(dossier.manifest.token_breakdown.cover_letter.cached_prompt_tokens)} cached · {formatTokens(dossier.manifest.token_breakdown.cover_letter.cache_write_tokens)} cache-write · {dossier.manifest.token_breakdown.cover_letter.reasoning_tokens == null ? `${formatTokens(dossier.manifest.token_breakdown.cover_letter.completion_tokens)} aggregate completion` : `${formatTokens(dossier.manifest.token_breakdown.cover_letter.visible_completion_tokens)} visible output · ${formatTokens(dossier.manifest.token_breakdown.cover_letter.reasoning_tokens)} reasoning`} tokens
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                </details>
              )}

              {outcomeResolutionRequired && onResolveOutcome && (
                <section
                  aria-label="Resolve unknown application outcome"
                  className="rounded-xl border border-warning/30 bg-warning/5 p-4"
                >
                  <div className="flex items-start gap-3">
                    <span className="mt-0.5 rounded-lg bg-warning/10 p-2 text-warning">
                      <CircleHelp className="h-4 w-4" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <h3 className="text-sm font-bold text-foreground">Decision required</h3>
                      <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
                        Hunter could not prove whether this application was submitted. Check the final screenshots,
                        recording, ATS account, and confirmation email before choosing.
                      </p>

                      {confirmingRetry ? (
                        <div className="mt-4 rounded-lg border border-destructive/25 bg-background p-3">
                          <p className="text-sm font-semibold text-foreground">Confirm that no application was submitted</p>
                          <p className="mt-1 text-xs text-muted-foreground">
                            Hunter will clear the safety guard and return this job to the Qualified queue. It will not start a new application.
                          </p>
                          <div className="mt-3 flex flex-wrap gap-2">
                            <button
                              type="button"
                              onClick={() => void resolveOutcome("not_submitted")}
                              disabled={resolvingOutcome !== null}
                              className="inline-flex items-center gap-1.5 rounded-lg bg-destructive px-3 py-2 text-xs font-semibold text-white disabled:opacity-50"
                            >
                              {resolvingOutcome === "not_submitted"
                                ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                : <RotateCcw className="h-3.5 w-3.5" />}
                              Move to Qualified
                            </button>
                            <button
                              type="button"
                              onClick={() => setConfirmingRetry(false)}
                              disabled={resolvingOutcome !== null}
                              className="btn-secondary px-3 py-2 text-xs"
                            >
                              Back
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div className="mt-4 flex flex-wrap gap-2">
                          <button
                            type="button"
                            onClick={() => void resolveOutcome("submitted")}
                            disabled={resolvingOutcome !== null}
                            className="inline-flex items-center gap-1.5 rounded-lg border border-success/25 bg-success/10 px-3 py-2 text-xs font-semibold text-success disabled:opacity-50"
                          >
                            {resolvingOutcome === "submitted"
                              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              : <CheckCircle2 className="h-3.5 w-3.5" />}
                            Mark as applied
                          </button>
                          <button
                            type="button"
                            onClick={() => setConfirmingRetry(true)}
                            disabled={resolvingOutcome !== null}
                            className="inline-flex items-center gap-1.5 rounded-lg border border-destructive/25 bg-background px-3 py-2 text-xs font-semibold text-destructive hover:bg-destructive/5 disabled:opacity-50"
                          >
                            <RotateCcw className="h-3.5 w-3.5" /> Mark as not applied
                          </button>
                          <button
                            type="button"
                            onClick={onClose}
                            disabled={resolvingOutcome !== null}
                            className="btn-secondary px-3 py-2 text-xs"
                          >
                            Keep unresolved
                          </button>
                        </div>
                      )}

                      {resolutionError && (
                        <p role="alert" className="mt-3 text-xs font-medium text-destructive">{resolutionError}</p>
                      )}
                    </div>
                  </div>
                </section>
              )}

              <div className="grid gap-5 xl:grid-cols-[minmax(0,1.25fr)_minmax(360px,0.75fr)]">
                <div className="space-y-5">
                  <section className="overflow-hidden rounded-xl border border-border bg-card">
                    <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
                      <div>
                        <h3 className="text-sm font-bold text-foreground">Visual evidence</h3>
                        <p className="text-xs text-muted-foreground">
                          {dossier.screenshots.length
                            ? `${screenshotIndex + 1} of ${dossier.screenshots.length} screenshots`
                            : "No screenshots captured"}
                        </p>
                      </div>
                      {dossier.screenshots.length > 1 && (
                        <div className="flex items-center gap-1">
                          <button
                            type="button"
                            onClick={() => setScreenshotIndex((current) => Math.max(0, current - 1))}
                            disabled={screenshotIndex === 0}
                            aria-label="Previous screenshot"
                            className="rounded-md p-2 text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-30"
                          >
                            <ChevronLeft className="h-4 w-4" />
                          </button>
                          <button
                            type="button"
                            onClick={() => setScreenshotIndex((current) => Math.min(dossier.screenshots.length - 1, current + 1))}
                            disabled={screenshotIndex === dossier.screenshots.length - 1}
                            aria-label="Next screenshot"
                            className="rounded-md p-2 text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-30"
                          >
                            <ChevronRight className="h-4 w-4" />
                          </button>
                        </div>
                      )}
                    </div>
                    <div className="flex min-h-72 items-center justify-center bg-secondary/60 p-3">
                      {screenshot.loading ? (
                        <Loader2 className="h-6 w-6 animate-spin text-primary" />
                      ) : screenshot.url ? (
                        <img src={screenshot.url} alt={`Application evidence ${screenshotIndex + 1}`} className="max-h-[560px] w-full rounded-lg object-contain" />
                      ) : (
                        <div className="text-center text-muted-foreground">
                          <ImageIcon className="mx-auto h-8 w-8" />
                          <p className="mt-2 text-sm">{screenshot.error ? "Screenshot could not be loaded" : "No visual evidence"}</p>
                        </div>
                      )}
                    </div>
                    {currentScreenshot && (
                      <p className="truncate border-t border-border px-4 py-2 font-mono text-[10px] text-muted-foreground">{currentScreenshot}</p>
                    )}
                  </section>

                  {dossier.recording && (
                    <section className="rounded-xl border border-border bg-card p-4">
                      <div className="flex items-center justify-between gap-3">
                        <div className="flex items-center gap-2">
                          <Film className="h-4 w-4 text-muted-foreground" />
                          <div>
                            <h3 className="text-sm font-bold text-foreground">Session recording</h3>
                            <p className="text-xs text-muted-foreground">Load only when you need the full replay.</p>
                          </div>
                        </div>
                        {!showRecording && (
                          <button type="button" onClick={() => setShowRecording(true)} className="btn-secondary px-3 py-1.5 text-xs">
                            Load video
                          </button>
                        )}
                      </div>
                      {showRecording && (
                        <div className="mt-4">
                          {recording.loading ? (
                            <div className="flex h-40 items-center justify-center bg-secondary"><Loader2 className="h-6 w-6 animate-spin text-primary" /></div>
                          ) : recording.url ? (
                            <video src={recording.url} controls className="max-h-[560px] w-full rounded-lg bg-black" />
                          ) : (
                            <p className="text-sm text-destructive">Recording could not be loaded.</p>
                          )}
                        </div>
                      )}
                    </section>
                  )}

                  {(dossier.manifest.confirmation_evidence || judgeNotes || issues.length > 0) && (
                    <section className="rounded-xl border border-border bg-card p-4">
                      <h3 className="text-sm font-bold text-foreground">Outcome review</h3>
                      {dossier.manifest.confirmation_evidence && (
                        <div className="mt-3">
                          <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Confirmation evidence</p>
                          <p className="mt-1 whitespace-pre-wrap text-sm text-foreground">{dossier.manifest.confirmation_evidence}</p>
                        </div>
                      )}
                      {judgeNotes && (
                        <div className="mt-3">
                          <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Judge notes</p>
                          <p className="mt-1 whitespace-pre-wrap text-sm text-foreground">{judgeNotes}</p>
                        </div>
                      )}
                      {issues.length > 0 && (
                        <div className="mt-3">
                          <p className="text-[10px] font-semibold uppercase tracking-wider text-destructive">Issues</p>
                          <ul className="mt-1 space-y-1 text-sm text-foreground">
                            {issues.map((issue) => <li key={issue}>• {issue}</li>)}
                          </ul>
                        </div>
                      )}
                    </section>
                  )}
                </div>

                <div className="space-y-5">
                  <section className="rounded-xl border border-border bg-card p-4">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <h3 className="text-sm font-bold text-foreground">Attempt details</h3>
                        <p className="font-mono text-[10px] text-muted-foreground">{dossier.manifest.attempt_id}</p>
                      </div>
                      <span className="rounded-full bg-secondary px-2.5 py-1 text-xs font-semibold capitalize text-foreground">{dossier.manifest.status}</span>
                    </div>
                    <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
                      <div><dt className="text-[10px] uppercase tracking-wider text-muted-foreground">Started</dt><dd>{formatDate(dossier.manifest.started_at)}</dd></div>
                      <div><dt className="text-[10px] uppercase tracking-wider text-muted-foreground">Finished</dt><dd>{formatDate(dossier.manifest.finished_at)}</dd></div>
                      <div><dt className="text-[10px] uppercase tracking-wider text-muted-foreground">Duration</dt><dd>{formatDuration(dossier.manifest.duration_seconds)}</dd></div>
                      <div><dt className="text-[10px] uppercase tracking-wider text-muted-foreground">Steps</dt><dd>{dossier.manifest.step_count}</dd></div>
                      <div><dt className="text-[10px] uppercase tracking-wider text-muted-foreground">Cost</dt><dd>{dossier.manifest.cost_usd == null ? "—" : `$${dossier.manifest.cost_usd.toFixed(4)}`}</dd></div>
                    </dl>
                    {dossier.manifest.job_url && (
                      <a href={dossier.manifest.job_url} target="_blank" rel="noopener noreferrer" className="mt-4 inline-flex items-center gap-1.5 text-xs font-semibold text-primary hover:underline">
                        Open job listing <ExternalLink className="h-3 w-3" />
                      </a>
                    )}
                    {dossier.original_listing_text && (
                      <details className="group mt-3 rounded-lg border border-border bg-background">
                        <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-semibold text-foreground">
                          View original listing text
                          <ChevronRight className="h-3.5 w-3.5 flex-shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
                        </summary>
                        <div className="max-h-96 overflow-y-auto border-t border-border px-3 py-3">
                          <p className="whitespace-pre-wrap text-sm leading-6 text-foreground">
                            {dossier.original_listing_text}
                          </p>
                        </div>
                      </details>
                    )}
                  </section>

                  <section className="rounded-xl border border-border bg-card p-4">
                    <h3 className="text-sm font-bold text-foreground">Dossier files</h3>
                    <div className="mt-2 divide-y divide-border">
                      {dossier.files.map((file) => (
                        <button
                          key={file.path}
                          type="button"
                          onClick={() => void downloadFile(file.path)}
                          disabled={downloadingPath === file.path}
                          className="flex w-full items-center gap-3 py-2.5 text-left hover:text-primary disabled:opacity-50"
                        >
                          <FileText className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-xs font-semibold">{file.label}</span>
                            <span className="block truncate font-mono text-[10px] text-muted-foreground">{file.path}</span>
                          </span>
                          <span className="text-[10px] text-muted-foreground">{formatSize(file.size_bytes)}</span>
                          {downloadingPath === file.path ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                        </button>
                      ))}
                    </div>
                  </section>

                  <section className="rounded-xl border border-border bg-card p-4">
                    <div className="flex items-baseline justify-between gap-3">
                      <h3 className="text-sm font-bold text-foreground">Application timeline</h3>
                      <span className="text-xs text-muted-foreground">{dossier.steps.length} steps</span>
                    </div>
                    {dossier.steps.length === 0 ? (
                      <p className="mt-3 text-sm text-muted-foreground">No structured timeline was recorded.</p>
                    ) : (
                      <div className="mt-3 max-h-[70vh] space-y-2 overflow-y-auto pr-1">
                        {dossier.steps.map((step) => (
                          <details key={step.number} className="group rounded-lg border border-border bg-background open:bg-secondary/50">
                            <summary className="cursor-pointer list-none px-3 py-2.5">
                              <div className="flex gap-3">
                                <span className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-secondary font-mono text-[10px] font-bold text-foreground">{step.number}</span>
                                <div className="min-w-0 flex-1">
                                  <p className="line-clamp-2 text-xs font-semibold text-foreground">{step.next_goal || step.evaluation || `Step ${step.number}`}</p>
                                  <p className="mt-0.5 text-[10px] text-muted-foreground">{step.elapsed_seconds == null ? "Time unavailable" : `+${step.elapsed_seconds.toFixed(1)}s`}</p>
                                </div>
                              </div>
                            </summary>
                            <div className="space-y-3 border-t border-border px-3 py-3 text-xs">
                              {step.evaluation && <div><p className="font-semibold text-muted-foreground">Previous result</p><p className="mt-0.5 whitespace-pre-wrap">{step.evaluation}</p></div>}
                              {step.next_goal && <div><p className="font-semibold text-muted-foreground">Next goal</p><p className="mt-0.5 whitespace-pre-wrap">{step.next_goal}</p></div>}
                              {step.url && <p className="break-all font-mono text-[10px] text-muted-foreground">{step.url}</p>}
                              {step.results.map((result, index) => (
                                <p key={`${result.kind}-${index}`} className={`whitespace-pre-wrap ${result.kind === "error" ? "text-destructive" : "text-foreground"}`}>{result.text}</p>
                              ))}
                              {step.actions.length > 0 && (
                                <details>
                                  <summary className="cursor-pointer font-semibold text-muted-foreground">Raw actions</summary>
                                  <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-md bg-foreground p-2 font-mono text-[10px] text-background">{JSON.stringify(step.actions, null, 2)}</pre>
                                </details>
                              )}
                              {step.screenshot && dossier.screenshots.includes(step.screenshot) && (
                                <button type="button" onClick={() => setScreenshotIndex(dossier.screenshots.indexOf(step.screenshot as string))} className="inline-flex items-center gap-1.5 font-semibold text-primary hover:underline">
                                  <ImageIcon className="h-3.5 w-3.5" /> Show screenshot
                                </button>
                              )}
                            </div>
                          </details>
                        ))}
                      </div>
                    )}
                  </section>
                </div>
              </div>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
