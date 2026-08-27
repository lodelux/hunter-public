import { useState } from "react";
import { AlertTriangle, ChevronDown, ChevronUp, ExternalLink, Link2, Loader2, Mail, RefreshCw, Settings2 } from "lucide-react";
import { Link } from "react-router-dom";
import {
  dismissGmailOutcomeReview,
  openTrustedExternalUrl,
} from "../../lib/api";
import type { GmailOutcomeStatus } from "../../lib/types";

interface GmailOutcomeReviewPanelProps {
  status: GmailOutcomeStatus | null;
  loading: boolean;
  loadError: string | null;
  onRefresh: () => Promise<void>;
}

function formatReceivedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Received date unavailable";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function GmailOutcomeReviewPanel({
  status,
  loading,
  loadError,
  onRefresh,
}: GmailOutcomeReviewPanelProps) {
  const [busyMessageId, setBusyMessageId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(true);

  const handleOpenGmail = async (url: string) => {
    setActionError(null);
    try {
      await openTrustedExternalUrl(url);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not open Gmail.");
    }
  };

  const handleMarkReviewed = async (messageId: string) => {
    setBusyMessageId(messageId);
    setActionError(null);
    try {
      await dismissGmailOutcomeReview(messageId);
      await onRefresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not mark the email as reviewed.");
    } finally {
      setBusyMessageId(null);
    }
  };

  const reviews = status?.review_required || [];

  return (
    <section aria-labelledby="gmail-review-title" className="overflow-hidden rounded-xl border border-border bg-card">
      <div className={`flex flex-col gap-4 bg-warning/5 p-5 sm:flex-row sm:items-start sm:justify-between ${collapsed ? "" : "border-b border-border"}`}>
        <div className="flex min-w-0 gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-warning/12 text-warning">
            <Mail className="h-5 w-5" />
          </span>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 id="gmail-review-title" className="section-title">Email outcomes needing review</h2>
              {!loading && reviews.length > 0 && (
                <span
                  aria-label={`${reviews.length} email review${reviews.length === 1 ? "" : "s"} waiting`}
                  className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-warning/15 px-1.5 text-[11px] font-semibold tabular-nums text-warning"
                >
                  {reviews.length}
                </span>
              )}
            </div>
            {!collapsed && (
              <p className="mt-1 max-w-2xl text-[13px] leading-5 text-muted-foreground">
                Hunter held these messages because the hiring outcome was not explicit enough to update automatically.
              </p>
            )}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          {!collapsed && (
            <>
              <button
                type="button"
                onClick={() => void onRefresh()}
                disabled={loading}
                className="btn-secondary"
              >
                {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                Refresh
              </button>
              <Link to="/settings?section=gmail" className="btn-secondary">
                <Settings2 className="h-4 w-4" /> Gmail settings
              </Link>
            </>
          )}
          <button
            type="button"
            aria-expanded={!collapsed}
            aria-controls="gmail-review-content"
            onClick={() => setCollapsed((value) => !value)}
            className="btn-secondary"
          >
            {collapsed ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}
            {collapsed ? "Show" : "Hide"}
          </button>
        </div>
      </div>

      {!collapsed && (
        <div id="gmail-review-content">
          {(loadError || actionError) && (
            <div className="m-4 error-banner">{actionError || loadError}</div>
          )}

          {loading && !status ? (
            <div className="flex h-40 items-center justify-center">
              <Loader2 className="h-7 w-7 animate-spin text-primary" />
            </div>
          ) : reviews.length === 0 ? (
            <div className="px-5 py-12 text-center">
              <span className="mx-auto flex h-11 w-11 items-center justify-center rounded-full bg-success/10 text-success">
                <Mail className="h-5 w-5" />
              </span>
              <h3 className="mt-3 text-sm font-semibold text-foreground">No email outcomes need review</h3>
              <p className="mt-1 text-[13px] text-muted-foreground">
                Ambiguous recruiting messages will appear here without changing a job automatically.
              </p>
            </div>
          ) : (
            <div className="divide-y divide-border">
              {reviews.map((item) => {
                const busy = busyMessageId === item.message_id;
                return (
                  <article key={item.message_id} className="p-5">
                    <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <p className="eyebrow text-warning">
                            {item.company || "Unmatched recruiting email"}
                            {item.title ? ` · ${item.title}` : ""}
                          </p>
                          {item.job_url && (
                            <span className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                              <Link2 className="h-3 w-3" /> Matched job
                            </span>
                          )}
                        </div>
                        <h3 className="mt-1 truncate text-sm font-semibold text-foreground">
                          {item.subject || "Recruiting email"}
                        </h3>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {item.sender || "Unknown sender"} · {formatReceivedAt(item.received_at)}
                        </p>

                        <div className="mt-3 flex gap-2.5 rounded-lg bg-secondary/70 px-3 py-2.5">
                          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                          <div>
                            <p className="text-xs font-semibold text-foreground">Why Hunter paused</p>
                            <p className="mt-0.5 text-xs leading-5 text-muted-foreground">{item.reason}</p>
                          </div>
                        </div>
                      </div>

                      <div className="flex shrink-0 flex-wrap gap-2 lg:justify-end">
                        <button
                          type="button"
                          onClick={() => void handleOpenGmail(item.gmail_url)}
                          className="btn-primary"
                        >
                          <ExternalLink className="h-4 w-4" /> Open Gmail
                        </button>
                        <button
                          type="button"
                          onClick={() => void handleMarkReviewed(item.message_id)}
                          disabled={busy}
                          className="btn-secondary disabled:opacity-50"
                        >
                          {busy && <Loader2 className="h-4 w-4 animate-spin" />}
                          Mark reviewed
                        </button>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          )}

          {reviews.length > 0 && (
            <p className="border-t border-border px-5 py-3 text-xs text-muted-foreground">
              Marking an email reviewed clears this alert only. It does not change the job’s hiring outcome.
            </p>
          )}
        </div>
      )}
    </section>
  );
}
