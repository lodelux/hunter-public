import { useState } from "react";
import {
  AlertTriangle,
  Brain,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  Undo2,
} from "lucide-react";
import { updateScreeningOverride } from "../../lib/api";
import type {
  AssessmentDimension,
  ClassificationFact,
  ClassificationSalaryFact,
  Job,
} from "../../lib/types";

const ASSESSMENT_LABELS = {
  role: "Role",
  skills: "Skills",
  experience: "Experience",
  location_work_mode: "Location & work mode",
  language_eligibility: "Language & eligibility",
  salary: "Salary",
} as const;

const FACT_LABELS = {
  seniority: "Seniority",
  experience_years: "Experience required",
  work_mode: "Work mode",
  education: "Education",
  sponsorship: "Sponsorship",
} as const;

const RATING_STYLES: Record<AssessmentDimension["rating"], string> = {
  excellent: "bg-success/12 text-success",
  good: "bg-primary/10 text-primary",
  partial: "bg-warning/12 text-warning",
  poor: "bg-destructive/10 text-destructive",
  unknown: "bg-secondary text-muted-foreground",
};

const SCREENING_STYLES = {
  qualified: "bg-success/12 text-success",
  review: "bg-warning/12 text-warning",
  rejected: "bg-destructive/10 text-destructive",
} as const;

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function confidenceLabel(confidence: number): string {
  return `${Math.round(confidence * 100)}% confident`;
}

function FactEvidence({ evidence }: { evidence: string[] }) {
  if (!evidence.length) return null;
  return (
    <ul className="mt-1 space-y-0.5">
      {evidence.map((item, index) => (
        <li
          key={`${item}-${index}`}
          className="text-[11px] leading-4 text-muted-foreground"
        >
          “{item}”
        </li>
      ))}
    </ul>
  );
}

function ScalarFact({
  label,
  fact,
  suffix,
}: {
  label: string;
  fact?: ClassificationFact;
  suffix?: string;
}) {
  const value = fact?.value;
  const shown =
    value === null || value === undefined || value === "unknown"
      ? "Not stated"
      : `${typeof value === "string" ? humanize(value) : value}${suffix || ""}`;

  return (
    <div className="min-w-0 rounded-lg border border-border/60 bg-card px-3 py-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          {label}
        </p>
        {fact && (
          <span className="text-[10px] tabular-nums text-muted-foreground">
            {confidenceLabel(fact.confidence)}
          </span>
        )}
      </div>
      <p className="mt-0.5 text-[13px] font-medium text-foreground">{shown}</p>
      {fact && <FactEvidence evidence={fact.evidence || []} />}
    </div>
  );
}

function ListFact({
  label,
  facts,
}: {
  label: string;
  facts?: ClassificationFact<string | null>[];
}) {
  return (
    <div className="rounded-lg border border-border/60 bg-card px-3 py-2.5">
      <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      {!facts?.length ? (
        <p className="text-[13px] text-muted-foreground">None stated</p>
      ) : (
        <div className="space-y-2">
          {facts.map((fact, index) => (
            <div key={`${fact.value}-${index}`}>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-[13px] font-medium text-foreground">
                  {fact.value || "Not stated"}
                </span>
                <span className="text-[10px] tabular-nums text-muted-foreground">
                  {confidenceLabel(fact.confidence)}
                </span>
              </div>
              <FactEvidence evidence={fact.evidence || []} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function SalaryFact({ fact }: { fact?: ClassificationSalaryFact }) {
  let value = "Not stated";
  if (fact && (fact.minimum !== null || fact.maximum !== null)) {
    const currency = fact.currency ? `${fact.currency} ` : "";
    const range =
      fact.minimum !== null && fact.maximum !== null
        ? `${fact.minimum.toLocaleString()}–${fact.maximum.toLocaleString()}`
        : (fact.minimum ?? fact.maximum)?.toLocaleString();
    const interval =
      fact.interval && fact.interval !== "unknown" ? ` / ${fact.interval}` : "";
    value = `${currency}${range}${interval}`;
  }

  return (
    <div className="rounded-lg border border-border/60 bg-card px-3 py-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          Salary
        </p>
        {fact && (
          <span className="text-[10px] tabular-nums text-muted-foreground">
            {confidenceLabel(fact.confidence)}
          </span>
        )}
      </div>
      <p className="mt-0.5 text-[13px] font-medium text-foreground">{value}</p>
      {fact && <FactEvidence evidence={fact.evidence || []} />}
    </div>
  );
}

export default function JobClassificationPanel({
  job,
  onScreeningChanged,
}: {
  job: Job;
  onScreeningChanged?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [updatingOverride, setUpdatingOverride] = useState(false);
  const [overrideError, setOverrideError] = useState<string | null>(null);
  const classification = job.classification;
  if (!classification) return null;

  if (classification.status === "running") {
    return (
      <div className="mt-3 flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
        Classifying with {classification.model}…
      </div>
    );
  }

  if (classification.status === "failed") {
    return (
      <div className="mt-3 flex items-start gap-2 rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
        <span>
          Classification failed
          {classification.error ? `: ${classification.error}` : ""}
        </span>
      </div>
    );
  }

  const effectiveScreening =
    job.screening?.override || job.screening?.status || "review";
  const score = classification.score;
  const facts = classification.facts;
  const assessment = classification.assessment;
  const hasQualifiedOverride = job.screening?.override === "qualified";

  const handleOverride = async () => {
    setUpdatingOverride(true);
    setOverrideError(null);
    try {
      await updateScreeningOverride(
        job.url,
        hasQualifiedOverride ? null : "qualified",
      );
      onScreeningChanged?.();
    } catch (error) {
      setOverrideError(
        error instanceof Error ? error.message : "Failed to update override",
      );
    } finally {
      setUpdatingOverride(false);
    }
  };

  return (
    <div className="mt-3 rounded-xl border border-border/70 bg-secondary/25">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full items-center gap-2.5 px-3 py-2.5 text-left hover:bg-secondary/50 transition-colors rounded-xl"
      >
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 flex-shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 flex-shrink-0 text-muted-foreground" />
        )}
        <Brain className="h-4 w-4 flex-shrink-0 text-primary" />
        <span className="text-xs font-semibold text-foreground">AI analysis</span>
        {score !== null && (
          <span className="rounded-full bg-foreground px-2 py-0.5 text-[11px] font-semibold tabular-nums text-background">
            {score}/100
          </span>
        )}
        <span
          className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${SCREENING_STYLES[effectiveScreening]}`}
        >
          {humanize(effectiveScreening)}
          {job.screening?.override ? " override" : ""}
        </span>
        <span className="ml-auto text-[11px] text-muted-foreground">
          {expanded ? "Hide details" : "View assessment & facts"}
        </span>
      </button>

      {expanded && (
        <div className="border-t border-border/60 px-3 pb-3 pt-3">
          <section>
            <h5 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              Fit assessment
            </h5>
            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
              {Object.entries(ASSESSMENT_LABELS).map(([key, label]) => {
                const dimension = assessment[key as keyof typeof assessment];
                if (!dimension) return null;
                return (
                  <div
                    key={key}
                    className="rounded-lg border border-border/60 bg-card px-3 py-2.5"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-xs font-semibold text-foreground">{label}</p>
                      <span
                        className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${RATING_STYLES[dimension.rating]}`}
                      >
                        {humanize(dimension.rating)}
                      </span>
                    </div>
                    <p className="mt-1.5 text-[11px] leading-4 text-muted-foreground">
                      {dimension.reason}
                    </p>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="mt-4">
            <h5 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              Extracted facts
            </h5>
            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
              {Object.entries(FACT_LABELS).map(([key, label]) => (
                <ScalarFact
                  key={key}
                  label={label}
                  fact={facts[key as keyof typeof FACT_LABELS] as ClassificationFact | undefined}
                  suffix={key === "experience_years" ? " years" : undefined}
                />
              ))}
              <SalaryFact fact={facts.salary} />
              <ListFact label="Required languages" facts={facts.required_languages} />
              <ListFact label="Preferred languages" facts={facts.preferred_languages} />
              <ListFact label="Required skills" facts={facts.required_skills} />
              <ListFact label="Preferred skills" facts={facts.preferred_skills} />
            </div>
          </section>

          {!!job.screening?.reasons?.length && (
            <section className="mt-4 rounded-lg bg-destructive/10 px-3 py-2.5">
              <h5 className="text-[11px] font-semibold uppercase tracking-wide text-destructive">
                Screening reasons
              </h5>
              <ul className="mt-1 space-y-1">
                {job.screening.reasons.map((reason, index) => (
                  <li key={`${reason.code || "reason"}-${index}`} className="text-xs text-destructive">
                    {reason.message || reason.code || "Hard filter failed"}
                    {typeof reason.confidence === "number"
                      ? ` (${confidenceLabel(reason.confidence)})`
                      : ""}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {job.screening?.status === "rejected" && (
            <div className="mt-3 flex items-center gap-2">
              <button
                type="button"
                onClick={handleOverride}
                disabled={updatingOverride}
                className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-semibold text-foreground transition-colors hover:bg-secondary disabled:opacity-50"
              >
                {updatingOverride ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : hasQualifiedOverride ? (
                  <Undo2 className="h-3.5 w-3.5" />
                ) : (
                  <CheckCircle2 className="h-3.5 w-3.5" />
                )}
                {hasQualifiedOverride ? "Remove override" : "Override rejection"}
              </button>
              {overrideError && (
                <span className="text-xs text-destructive">{overrideError}</span>
              )}
            </div>
          )}

          <p className="mt-3 text-[10px] text-muted-foreground">
            {classification.model} · schema v{classification.schema_version} ·{" "}
            {new Date(classification.classified_at).toLocaleString()}
          </p>
        </div>
      )}
    </div>
  );
}
