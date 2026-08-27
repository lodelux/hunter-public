export interface SalaryExpectation {
  min: number;
  currency: string;
  period: "annual" | "monthly";
}

export interface CandidateProfile {
  country: string;
  target_job_titles: string[];
  target_locations: string[];
  blacklisted_companies: string[];
  languages: string[];
  visa_sponsorship_needed: boolean;
  salary_expectation: SalaryExpectation;
  markdown: string;
}

// ── LLM Settings ──────────────────────────────────────────────────────────
export type LLMProvider = "openai" | "anthropic" | "gemini" | "bedrock" | "ollama" | "openrouter" | "openai_compatible";

export interface LLMSettings {
  provider: LLMProvider;
  openai?: {
    api_key: string;
    model: string;
  };
  anthropic?: {
    api_key: string;
    model: string;
  };
  gemini?: {
    api_key: string;
    model: string;
  };
  bedrock?: {
    auth_mode: string;
    profile_name: string;
    access_key: string;
    secret_key: string;
    region: string;
    model: string;
  };
  ollama?: {
    base_url: string;
    model: string;
  };
  openrouter?: {
    api_key: string;
    model: string;
  };
  openai_compatible?: {
    base_url: string;
    api_key: string;
    model: string;
  };
}

// ── App Settings ──────────────────────────────────────────────────────────
export interface AppSettings {
  resume_path: string;
  blocked_domains: string[];
  sensitive_data: {
    email: string;
    password: string;
  };
  max_failures: number;
  stagger_delay: number;
  browser_headless: boolean;
  dossier_retention_enabled: boolean;
  dossier_retention_days: number;
  dossier_delete_after_days: number;
  auto_reject_after_months: number;
  data_dir: string;
  theme?: "light" | "dark" | "system";
}

export interface ManualBrowserStatus {
  state: "stopped" | "starting" | "running" | "stopping" | "error";
  message: string | null;
  started_at: string | null;
  supported: boolean;
  dependencies_ready: boolean;
  missing_dependencies: string[];
  display: string;
  vnc_port: number;
}

export type AutomationSource = "linkedin" | "indeed";

export interface AutomationConfig {
  enabled: boolean;
  sources: AutomationSource[];
  interval_minutes: 5 | 10 | 15 | 30 | 60;
  daily_job_limit: number;
  daily_company_limit: number;
  max_jobs_per_source: number;
  max_applications_per_cycle: number;
  automation_min_score: number;
  linkedin_outreach_enabled: boolean;
  hours_old: number;
  title: string;
  location: string;
  job_type: string;
  is_remote: boolean;
  linkedin_search_url: string;
}

export interface AutomationStatus {
  state: "stopped" | "running" | "paused";
  phase: "idle" | "waiting" | "collecting" | "classifying" | "applying";
  current_source: AutomationSource | null;
  current_job_url: string | null;
  next_run_at: string | null;
  last_cycle_started_at: string | null;
  last_cycle_finished_at: string | null;
  today_started: number;
  today_limit: number;
  queued_count: number;
  captcha_count: number;
  pause_reason: string | null;
  log: string[];
}

export interface AutomationResponse {
  config: AutomationConfig;
  status: AutomationStatus;
}

export interface GmailOutcomeReview {
  message_id: string;
  thread_id: string;
  received_at: string;
  subject: string;
  sender: string;
  reason: string;
  gmail_url: string;
  job_url: string | null;
  company: string | null;
  title: string | null;
}

export interface GmailOutcomeStatus {
  client_configured: boolean;
  connected: boolean;
  enabled: boolean;
  account_email: string | null;
  last_success_at: string | null;
  last_error: string | null;
  last_sync_summary: {
    scanned: number;
    updated: number;
    review_required: number;
  } | null;
  review_required_count: number;
  review_required: GmailOutcomeReview[];
  recent_events: Array<Record<string, unknown>>;
  telegram_configured: boolean;
}

export interface GmailOutcomeEvidence {
  source: "gmail";
  message_id: string;
  thread_id: string;
  received_at: string;
  gmail_url: string;
  outcome: "online_assessment" | "rejected" | "interview" | "offer";
  match_method: "company" | "company_and_role" | "manual";
  reason: string;
  evidence: string;
  subject: string;
}

export interface TimeoutOutcomeEvidence {
  source: "timeout";
  applied_at: string;
  rejected_at: string;
  months_without_outcome: number;
  reason: string;
}

// ── Job ───────────────────────────────────────────────────────────────────
export interface ClassificationFact<T = string | number | null> {
  value: T;
  confidence: number;
  evidence: string[];
}

export interface ClassificationSalaryFact {
  minimum: number | null;
  maximum: number | null;
  currency: string | null;
  interval: string | null;
  confidence: number;
  evidence: string[];
}

export interface ClassificationFacts {
  date_posted: ClassificationFact<string | null>;
  seniority: ClassificationFact<string | null>;
  experience_years: ClassificationFact<number | null>;
  work_mode: ClassificationFact<string | null>;
  required_languages: ClassificationFact<string | null>[];
  preferred_languages: ClassificationFact<string | null>[];
  required_skills: ClassificationFact<string | null>[];
  preferred_skills: ClassificationFact<string | null>[];
  education: ClassificationFact<string | null>;
  salary: ClassificationSalaryFact;
  sponsorship: ClassificationFact<string | null>;
}

export interface AssessmentDimension {
  rating: "excellent" | "good" | "partial" | "poor" | "unknown";
  reason: string;
}

export interface FitAssessment {
  role: AssessmentDimension;
  skills: AssessmentDimension;
  experience: AssessmentDimension;
  location_work_mode: AssessmentDimension;
  language_eligibility: AssessmentDimension;
  salary: AssessmentDimension;
}

export interface ScreeningReason {
  code?: string;
  message?: string;
  field?: string;
  confidence?: number | null;
}

export const JOB_CATEGORY_OPTIONS = [
  { value: "review", label: "Review" },
  { value: "qualified", label: "Qualified" },
  { value: "unqualified", label: "Ineligible" },
  { value: "applied", label: "Applied" },
  { value: "online_assessment", label: "Online assessment" },
  { value: "rejected", label: "Rejected" },
  { value: "interview", label: "Interview" },
  { value: "offer", label: "Offer" },
  { value: "accepted", label: "Accepted" },
  { value: "refused", label: "Refused" },
  { value: "failed", label: "Failed" },
  { value: "blocked", label: "Dismissed" },
] as const;

export type JobCategory = (typeof JOB_CATEGORY_OPTIONS)[number]["value"];

export interface JobStatusHistoryEntry {
  status: JobCategory;
  changed_at: string;
  source: string;
}

export interface Job {
  url: string;
  title: string;
  company: string;
  location: string;
  easy_apply: boolean | null;
  status: "pending" | "in_progress" | "applied" | "failed" | "blocked";
  category: JobCategory;
  source?: string;
  source_id?: string;
  direct_url?: string;
  date_posted?: string;
  job_type?: string;
  is_remote?: boolean;
  job_level?: string | null;
  job_function?: string | null;
  company_industry?: string | null;
  salary_source?: string | null;
  interval?: string | null;
  min_amount?: number | null;
  max_amount?: number | null;
  currency?: string | null;
  search_title?: string;
  search_location?: string;
  collected_at?: string;
  applied_at?: string;
  application_status?: ApplicationStatus | null;
  application_status_updated_at?: string | null;
  application_status_source?: "manual" | "gmail" | "timeout" | null;
  application_status_evidence?: GmailOutcomeEvidence | TimeoutOutcomeEvidence | null;
  gmail_outcome_evidence?: GmailOutcomeEvidence[];
  status_history?: JobStatusHistoryEntry[];
  error?: string;
  last_application_outcome?: {
    type?: string;
    retryable?: boolean;
    submission_confirmed?: boolean;
    attempt_id?: string | null;
  } | null;
  description?: string;
  tailored_resume_path?: string;
  cover_letter?: string;
  cover_letter_path?: string;
  cover_letter_pdf_path?: string;
  classification?: {
    status: "running" | "complete" | "failed";
    model: string;
    schema_version: number;
    classified_at: string;
    description_hash: string;
    profile_hash: string;
    facts: Partial<ClassificationFacts>;
    assessment: Partial<FitAssessment>;
    score: number | null;
    cost_usd?: number | null;
    error: string | null;
  };
  screening?: {
    status: "qualified" | "review" | "rejected";
    reasons: ScreeningReason[];
    policy_version: number;
    evaluated_at: string;
    override: "review" | "qualified" | "rejected" | null;
  };
}

export type ApplicationOutcomeDecision = "not_submitted" | "submitted";

// ── Resume Tailoring ─────────────────────────────────────────────────────
export interface TailorResult {
  url: string;
  status: "done" | "error";
  path?: string;
  content?: string;
  message?: string;
}

// ── Plugins ──────────────────────────────────────────────────────────────
export interface PluginFilter {
  key: string;
  label: string;
  type: "select" | "text";
  options: { value: string; label: string }[];
  url_param: string;
  default: string;
}

export interface PluginConfig {
  name: string;
  display_name: string;
  version: string;
  author: string;
  description: string;
  countries: string[];
  website: string;
  requires_login: boolean;
  login_url: string;
  is_builtin: boolean;
  enabled: boolean;
  filters: PluginFilter[];
}

// ── Country Config ───────────────────────────────────────────────────────
export interface CountryConfig {
  name: string;
  flag: string;
  date_format: string;
  currency: string;
  salary_period: "annual" | "monthly";
  address_labels: { state: string; zip: string };
  work_auth_options: string[];
  show_notice_period: boolean;
  show_nationality: boolean;
  show_cover_letter: boolean;
  show_photo: boolean;
  show_date_of_birth: boolean;
  phone_prefix: string;
  default_sources: string[];
}

export type CountryCode = string;

// ── Memory ────────────────────────────────────────────────────────────────
export interface Memory {
  id: number;
  website_domain: string;
  ats_platform: string | null;
  category: string;
  content: string;
  success: boolean;
  confidence: number;
  job_url: string;
  created_at: string;
  updated_at: string;
  access_count: number;
  agent_marked_critical?: boolean;
  critical?: boolean;
  critical_rank?: number | null;
  critical_reason?: string;
  source_attempt_id?: string;
  estimated_tokens?: number;
  scope?: string;
}

export interface MemoryStats {
  total_memories: number;
  unique_domains: number;
  by_category: Record<string, number>;
  critical_memories?: number;
  critical_max_count?: number;
  critical_max_tokens?: number;
}

export interface CriticalMemorySettings {
  auto_rerank: boolean;
  max_count: number;
  max_tokens: number;
}

// ── Metrics / Dashboard ───────────────────────────────────────────────────
export interface OverallStats {
  total_runs: number;
  successes: number;
  failures: number;
  success_rate: number;
  avg_duration: number;
  avg_steps: number;
  total_cost: number;
  first_run: string;
  last_run: string;
}

export interface CostBreakdown {
  classification: number;
  cover_letter: number;
  outreach?: number;
  cv: number;
  application_agent: number;
  judge: number;
  memory_extract: number;
}

export interface TokenUsageBreakdown {
  prompt_tokens: number;
  cached_prompt_tokens: number;
  cache_write_tokens: number;
  completion_tokens: number;
  visible_completion_tokens: number;
  reasoning_tokens: number | null;
  total_tokens: number;
  requests: number;
  models: Record<string, {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    requests: number;
  }>;
}

export interface RunMetric {
  id: number;
  run_id?: string | null;
  job_url: string;
  job_title: string;
  company: string;
  website_domain: string;
  ats_platform: string | null;
  success: boolean;
  error_message: string | null;
  started_at: string;
  finished_at: string;
  duration_seconds: number;
  step_count: number;
  memories_injected: number;
  memories_extracted: number;
  cost_usd: number | null;
  cost_breakdown?: CostBreakdown | null;
  token_breakdown?: Record<string, TokenUsageBreakdown> | null;
  created_at: string;
}

export interface ApplicationAttemptManifest {
  attempt_id: string;
  status: string;
  job_url: string;
  job_title: string;
  company: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  step_count: number;
  agent_claimed_success: boolean | null;
  judge_verdict: boolean | null;
  submission_confirmed: boolean;
  linkedin_outreach?: {
    status: "sent" | "not_applicable" | "failed" | "unknown";
    recipient?: string | null;
    profile_url?: string | null;
    cv_attached?: boolean;
    evidence?: string | null;
    reason?: string | null;
    recorded_at?: string;
  } | null;
  judgement: Record<string, unknown> | null;
  confirmation_evidence: string | null;
  error: string | null;
  errors: string[];
  recording_error: string | null;
  cost_usd: number | null;
  cost_breakdown: CostBreakdown | null;
  token_breakdown?: Record<string, TokenUsageBreakdown> | null;
}

export interface GenerationAuditReference {
  id: string;
  text: string;
}

export interface GenerationAuditDecision {
  decision: string;
  requirements: GenerationAuditReference[];
  evidence: GenerationAuditReference[];
}

export interface ResumeGenerationAudit {
  model: string;
  reasoning_effort: string;
  positioning: string;
  headline: {
    text: string;
    requirements: GenerationAuditReference[];
  };
  priorities: GenerationAuditDecision[];
  selections: Array<{
    section: string;
    label: string;
    text: string;
    relevance: number;
    requirements: GenerationAuditReference[];
    evidence: GenerationAuditReference[];
  }>;
}

export interface CoverLetterGenerationAudit {
  model: string;
  reasoning_effort: string;
  positioning: string;
  decisions: GenerationAuditDecision[];
}

export interface DocumentGenerationAudit {
  resume: ResumeGenerationAudit | null;
  cover_letter: CoverLetterGenerationAudit | null;
}

export interface ApplicationAttemptStep {
  number: number;
  elapsed_seconds: number | null;
  url: string | null;
  evaluation: string | null;
  next_goal: string | null;
  actions: unknown[];
  results: Array<{ kind: "error" | "result"; text: string }>;
  screenshot: string | null;
}

export interface ApplicationAttemptFile {
  label: string;
  path: string;
  size_bytes: number | null;
}

export interface ApplicationAttemptDossier {
  manifest: ApplicationAttemptManifest;
  steps: ApplicationAttemptStep[];
  screenshots: string[];
  recording: string | null;
  files: ApplicationAttemptFile[];
  generation_audit?: DocumentGenerationAudit | null;
  original_listing_text?: string | null;
}

export interface ApplicationAttemptStorageCandidate {
  attempt_id: string;
  job_title: string;
  company: string;
  status: string;
  started_at: string | null;
  action: "remove_video" | "delete_dossier";
  reclaimable_bytes: number;
}

export interface ApplicationAttemptStorage {
  root: string;
  video_retention_days: number;
  dossier_retention_days: number;
  total_dossiers: number;
  total_bytes: number;
  video_bytes: number;
  video_cleanup_dossiers: number;
  video_cleanup_bytes: number;
  dossier_cleanup_dossiers: number;
  dossier_cleanup_bytes: number;
  reclaimable_dossiers: number;
  reclaimable_bytes: number;
  protected_incomplete_dossiers: number;
  candidates: ApplicationAttemptStorageCandidate[];
}

export interface ApplicationAttemptCleanupResult {
  success: boolean;
  compacted_dossiers: number;
  deleted_dossiers: number;
  removed_bytes: number;
  removed_files: number;
  storage: ApplicationAttemptStorage;
}

// ── API Response ──────────────────────────────────────────────────────────
export interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: string;
}

export interface HealthChecks {
  database: boolean;
  chromium: boolean;
  chromium_installing: boolean;
  llm_configured: boolean;
  worker_running: boolean;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  version: string;
  checks: HealthChecks;
  build_id?: string;
}

export interface SystemUsage {
  percent: number;
  used_bytes: number;
  total_bytes: number;
}

export interface SystemStatusResponse {
  collected_at: string;
  cpu: { percent: number };
  memory: SystemUsage & { available_bytes: number };
  swap: SystemUsage;
  disk: SystemUsage & { free_bytes: number; hunter_data_bytes: number };
  temperature: {
    available: boolean;
    celsius: number | null;
    sensor: string | null;
    high_celsius: number | null;
    critical_celsius: number | null;
  };
  headroom: {
    state: "healthy" | "watch" | "critical";
    summary: string;
  };
}

// ── Status Responses ─────────────────────────────────────────────────────
export interface CollectionStatus {
  running: boolean;
  title: string | null;
  log: string[];
  collected: number;
  max_jobs: number;
  classification_running: boolean;
  classification_total: number;
  classification_completed: number;
  classification_failed: number;
  error: string | null;
  finished_at: string | null;
}

export interface ApplyStatus {
  running: boolean;
  mode: string | null;
  workers: number;
  log: string[];
  error: string | null;
  finished_at: string | null;
}

export type JobStatus = "pending" | "in_progress" | "applied" | "failed" | "blocked";

export type ApplicationStatus =
  | "applied"
  | "online_assessment"
  | "rejected"
  | "interview"
  | "offer"
  | "accepted"
  | "refused";

export interface JobStats {
  total: number;
  pending: number;
  applied: number;
  failed: number;
  blocked: number;
  in_progress: number;
  category_counts: Record<JobCategory, number>;
  qualified?: number;
  review?: number;
  rejected?: number;
  application_applied?: number;
  application_online_assessment?: number;
  application_rejected?: number;
  application_interview?: number;
  application_offer?: number;
  application_accepted?: number;
  application_refused?: number;
}

export interface DashboardResponse {
  jobs: JobStats;
  memory: MemoryStats;
  metrics?: Record<string, unknown>;
  gmail_outcomes?: GmailOutcomeStatus;
}

export interface DomainInfo {
  website_domain: string;
  ats_platform: string | null;
  count: number;
  avg_confidence: number;
  success_count: number;
  failure_count: number;
  critical_count?: number;
}

export interface SetupStatus {
  onboarding_completed: boolean;
  has_profile: boolean;
  has_llm: boolean;
  has_resume: boolean;
  has_jobs: boolean;
}

// ── Q&A Repository ───────────────────────────────────────────────────────
export interface QAEntry {
  id: number;
  question: string;
  normalized: string;
  answer: string;
  question_type: string;
  source_domain: string;
  times_seen: number;
  merged_into_id: number | null;
  created_at: string;
  updated_at: string;
  reviewed: boolean;
}

export interface QAStats {
  total: number;
  answered: number;
  unanswered: number;
  reviewed: number;
  to_review: number;
}

// ── Run Logs ─────────────────────────────────────────────────────────────
export interface RunLog {
  id: number;
  run_id: string;
  job_url: string | null;
  timestamp: string;
  level: string;
  message: string;
  created_at: string;
}

export interface RunWithLogs extends RunMetric {
  run_id: string | null;
  log_count: number;
}
