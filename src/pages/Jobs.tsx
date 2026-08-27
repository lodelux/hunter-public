import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArchiveX, Bot, ChevronRight, Inbox, Layers3, Target } from "lucide-react";
import { getGmailOutcomeStatus, getJobStats } from "../lib/api";
import { PageHeader } from "../components/ui";
import { JOB_CATEGORY_OPTIONS } from "../lib/types";
import type { GmailOutcomeStatus, JobCategory, JobStats } from "../lib/types";
import CollectTab from "./jobs/CollectTab";
import ReviewApplyTab from "./jobs/ReviewApplyTab";
import GmailOutcomeReviewPanel from "../components/jobs/GmailOutcomeReviewPanel";

type TabId = "collect" | "pending";
type CategoryGroupId = "queue" | "applications" | "outcomes";

const EMPTY_CATEGORY_COUNTS = Object.fromEntries(
  JOB_CATEGORY_OPTIONS.map(({ value }) => [value, 0]),
) as Record<JobCategory, number>;

const EMPTY_STATS: JobStats = {
  total: 0,
  pending: 0,
  applied: 0,
  failed: 0,
  blocked: 0,
  in_progress: 0,
  category_counts: EMPTY_CATEGORY_COUNTS,
};

const CATEGORY_LABELS = Object.fromEntries(
  JOB_CATEGORY_OPTIONS.map(({ value, label }) => [value, label]),
) as Record<JobCategory, string>;

const CATEGORY_GROUPS: Array<{
  id: CategoryGroupId;
  label: string;
  description: string;
  icon: typeof Inbox;
  categories: JobCategory[];
}> = [
  {
    id: "queue",
    label: "Review queue",
    description: "Jobs that need a decision",
    icon: Inbox,
    categories: ["review", "qualified"],
  },
  {
    id: "applications",
    label: "Applications",
    description: "Attempts and exceptions",
    icon: Layers3,
    categories: ["applied", "failed"],
  },
  {
    id: "outcomes",
    label: "Outcomes",
    description: "Progress and decisions",
    icon: Target,
    categories: ["online_assessment", "rejected", "interview", "offer", "accepted", "refused"],
  },
];

const EXCLUDED_CATEGORIES: JobCategory[] = ["unqualified", "blocked"];

const VALID_CATEGORIES = new Set<JobCategory>(JOB_CATEGORY_OPTIONS.map(({ value }) => value));

function categoryFromQuery(value: string | null): JobCategory {
  return value && VALID_CATEGORIES.has(value as JobCategory) ? value as JobCategory : "review";
}

export default function Jobs() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const activeTab: TabId = requestedTab === "pending" || requestedTab === "history" ? "pending" : "collect";
  const requestedCategory = categoryFromQuery(searchParams.get("category"));
  const [reviewCategory, setReviewCategory] = useState<JobCategory>(requestedCategory);
  const [stats, setStats] = useState<JobStats>(EMPTY_STATS);
  const [gmailOutcomes, setGmailOutcomes] = useState<GmailOutcomeStatus | null>(null);
  const [gmailLoading, setGmailLoading] = useState(true);
  const [gmailError, setGmailError] = useState<string | null>(null);

  useEffect(() => {
    setReviewCategory(requestedCategory);
  }, [requestedCategory]);

  const fetchStats = useCallback(() => {
    getJobStats()
      .then((response) => setStats(response || EMPTY_STATS))
      .catch(() => {});
  }, []);

  const refreshGmailOutcomes = useCallback(async () => {
    setGmailLoading(true);
    try {
      setGmailOutcomes(await getGmailOutcomeStatus());
      setGmailError(null);
    } catch {
      setGmailError("Could not load Gmail outcome reviews.");
    } finally {
      setGmailLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStats();
    void refreshGmailOutcomes();
  }, [fetchStats, refreshGmailOutcomes]);

  useEffect(() => {
    const refreshOnFocus = () => void refreshGmailOutcomes();
    window.addEventListener("focus", refreshOnFocus);
    return () => window.removeEventListener("focus", refreshOnFocus);
  }, [refreshGmailOutcomes]);

  const handleJobsChanged = useCallback(() => {
    fetchStats();
  }, [fetchStats]);

  const selectTab = (tab: TabId) => {
    navigate(tab === "collect" ? "/jobs" : `/jobs?tab=pending&category=${reviewCategory}`, { replace: true });
  };

  const selectCategory = (category: JobCategory) => {
    setReviewCategory(category);
    navigate(`/jobs?tab=pending&category=${category}`, { replace: true });
  };

  const activeGroup = CATEGORY_GROUPS.find((group) => group.categories.includes(reviewCategory));
  const folderCategories = activeGroup?.categories || EXCLUDED_CATEGORIES;
  const groupTotals = useMemo(() => Object.fromEntries(
    CATEGORY_GROUPS.map((group) => [
      group.id,
      group.categories.reduce((total, category) => total + (stats.category_counts?.[category] || 0), 0),
    ]),
  ) as Record<CategoryGroupId, number>, [stats]);
  const excludedTotal = EXCLUDED_CATEGORIES.reduce(
    (total, category) => total + (stats.category_counts?.[category] || 0),
    0,
  );

  return (
    <div>
      <PageHeader
        title="Runner & jobs"
        subtitle={`${stats.total || 0} discovered · ${stats.category_counts?.qualified || 0} qualified · ${stats.applied || 0} applied`}
      />

      <div role="tablist" aria-label="Jobs workspace" className="mb-6 inline-flex rounded-lg bg-secondary p-1">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "collect"}
          onClick={() => selectTab("collect")}
          className={`flex min-w-[170px] items-center gap-3 rounded-md px-4 py-2.5 text-left transition-colors ${
            activeTab === "collect" ? "bg-card text-foreground" : "text-muted-foreground hover:text-foreground"
          }`}
        >
          <Bot className={`h-4 w-4 ${activeTab === "collect" ? "text-primary" : ""}`} />
          <span>
            <span className="block text-sm font-semibold">Autonomous runner</span>
            <span className="block text-[11px]">Control and discovery</span>
          </span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "pending"}
          onClick={() => selectTab("pending")}
          className={`flex min-w-[170px] items-center gap-3 rounded-md px-4 py-2.5 text-left transition-colors ${
            activeTab === "pending" ? "bg-card text-foreground" : "text-muted-foreground hover:text-foreground"
          }`}
        >
          <Inbox className={`h-4 w-4 ${activeTab === "pending" ? "text-primary" : ""}`} />
          <span className="flex-1">
            <span className="block text-sm font-semibold">Review jobs</span>
            <span className="block text-[11px]">Exceptions and outcomes</span>
          </span>
          {groupTotals.queue + stats.failed + (gmailOutcomes?.review_required_count || 0) > 0 && (
            <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-semibold text-primary">
              {groupTotals.queue + stats.failed + (gmailOutcomes?.review_required_count || 0)}
            </span>
          )}
        </button>
      </div>

      {activeTab === "collect" && <CollectTab onJobsChanged={handleJobsChanged} />}

      {activeTab === "pending" && (
        <>
          <div className="mb-4 grid gap-3 md:grid-cols-3" aria-label="Job stages">
            {CATEGORY_GROUPS.map((group) => {
              const Icon = group.icon;
              const active = activeGroup?.id === group.id;
              return (
                <button
                  key={group.id}
                  type="button"
                  onClick={() => selectCategory(group.categories[0])}
                  className={`flex items-center gap-3 rounded-xl border p-4 text-left transition-colors ${
                    active
                      ? "border-primary/40 bg-primary/8"
                      : "border-border bg-card hover:bg-secondary/60"
                  }`}
                >
                  <span className={`flex h-9 w-9 items-center justify-center rounded-lg ${active ? "bg-primary/12 text-primary" : "bg-secondary text-muted-foreground"}`}>
                    <Icon className="h-4 w-4" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-semibold text-foreground">{group.label}</span>
                    <span className="block truncate text-xs text-muted-foreground">{group.description}</span>
                  </span>
                  <span className="flex shrink-0 flex-col items-end">
                    <span className="text-lg font-semibold tabular-nums text-foreground">{groupTotals[group.id]}</span>
                    {group.id === "outcomes" && (gmailOutcomes?.review_required_count || 0) > 0 && (
                      <span className="mt-0.5 rounded-full bg-warning/12 px-1.5 py-0.5 text-[10px] font-semibold text-warning">
                        {gmailOutcomes?.review_required_count} to review
                      </span>
                    )}
                  </span>
                  <ChevronRight className={`h-4 w-4 ${active ? "text-primary" : "text-muted-foreground"}`} />
                </button>
              );
            })}
          </div>

          <div className="mb-4 flex justify-end">
            <button
              type="button"
              onClick={() => selectCategory(
                EXCLUDED_CATEGORIES.includes(reviewCategory) ? reviewCategory : "unqualified",
              )}
              className={`inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors ${
                !activeGroup
                  ? "border-border bg-secondary text-foreground"
                  : "border-transparent text-muted-foreground hover:border-border hover:bg-secondary/60 hover:text-foreground"
              }`}
            >
              <ArchiveX className="h-4 w-4" />
              <span className="font-semibold">Excluded</span>
              <span className="tabular-nums text-muted-foreground">{excludedTotal}</span>
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>

          <div
            role="tablist"
            aria-label="Job folders"
            className="scrollbar-hidden mb-5 flex gap-1 overflow-x-auto border-b border-border"
          >
            {folderCategories.map((category) => {
              const active = reviewCategory === category;
              return (
                <button
                  key={category}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  aria-label={`${CATEGORY_LABELS[category]} (${stats.category_counts?.[category] || 0})`}
                  onClick={() => selectCategory(category)}
                  className={`min-w-max border-b-2 px-3 py-2.5 text-sm font-semibold transition-colors ${
                    active
                      ? "border-primary text-foreground"
                      : "border-transparent text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {CATEGORY_LABELS[category]}
                  <span className={`ml-1.5 text-xs tabular-nums ${active ? "text-primary" : "text-muted-foreground"}`}>
                    {stats.category_counts?.[category] || 0}
                  </span>
                </button>
              );
            })}
          </div>

          <div className="mb-8">
            <GmailOutcomeReviewPanel
              status={gmailOutcomes}
              loading={gmailLoading}
              loadError={gmailError}
              onRefresh={refreshGmailOutcomes}
            />
          </div>

          <ReviewApplyTab category={reviewCategory} onJobsChanged={handleJobsChanged} />
        </>
      )}
    </div>
  );
}
