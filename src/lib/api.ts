/** API client for the same-origin Python backend. */
import type {
  ApplicationOutcomeDecision,
  CandidateProfile,
  LLMSettings,
  AppSettings,
  Job,
  JobStats,
  MemoryStats,
  Memory,
  CriticalMemorySettings,
  HealthResponse,
  RunMetric,
  CollectionStatus,
  ApplyStatus,
  DashboardResponse,
  DomainInfo,
  RunLog,
  RunWithLogs,
  ApplicationStatus,
  JobCategory,
  QAEntry,
  QAStats,
  AutomationConfig,
  AutomationResponse,
  ApplicationAttemptDossier,
  ApplicationAttemptCleanupResult,
  ApplicationAttemptStorage,
  GmailOutcomeStatus,
  ManualBrowserStatus,
  SystemStatusResponse,
} from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

function getBaseUrl(): string {
  return API_BASE_URL;
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const url = `${getBaseUrl()}${path}`;
  const res = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "Unknown error");
    throw new Error(`API Error ${res.status}: ${text}`);
  }

  const text = await res.text();
  if (!text) return {} as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(`Invalid JSON response from ${path}`);
  }
}

async function requestBlob(path: string): Promise<Blob> {
  const res = await fetch(`${getBaseUrl()}${path}`);
  if (!res.ok) {
    const text = await res.text().catch(() => "Unknown error");
    throw new Error(`API Error ${res.status}: ${text}`);
  }
  return res.blob();
}

async function uploadFile<T>(path: string, file: File): Promise<T> {
  const res = await fetch(`${getBaseUrl()}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": file.type || "application/octet-stream",
      "X-Hunter-Filename": encodeURIComponent(file.name),
    },
    body: file,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "Unknown error");
    throw new Error(`API Error ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

// ── Health ────────────────────────────────────────────────────────────────
export async function checkHealth() {
  return request<HealthResponse>("/health");
}

export async function getSystemStatus() {
  return request<SystemStatusResponse>("/system/status");
}

export async function getChromiumStatus() {
  const res = await fetch(`${getBaseUrl()}/chromium/status`);
  return res.json() as Promise<{ state: string; message: string }>;
}

// ── Profile ───────────────────────────────────────────────────────────────
export async function getProfile() {
  return request<CandidateProfile>("/profile");
}

export async function saveProfile(profile: CandidateProfile) {
  return request<{ success: boolean }>("/profile", {
    method: "PUT",
    body: JSON.stringify(profile),
  });
}

// ── LLM Settings ──────────────────────────────────────────────────────────
export async function getLLMSettings() {
  return request<LLMSettings>("/settings/llm");
}

export async function saveLLMSettings(settings: LLMSettings) {
  return request<{ success: boolean }>("/settings/llm", {
    method: "PUT",
    body: JSON.stringify(settings),
  });
}

export async function testLLMConnection(settings: LLMSettings) {
  return request<{ success: boolean; message: string }>("/llm/test", {
    method: "POST",
    body: JSON.stringify(settings),
  });
}

export async function fetchOllamaModels(baseUrl: string) {
  return request<{ success: boolean; models: string[]; message?: string }>("/llm/ollama-models", {
    method: "POST",
    body: JSON.stringify({ base_url: baseUrl }),
  });
}

// ── App Settings ──────────────────────────────────────────────────────────
export async function getSettings() {
  return request<AppSettings>("/settings");
}

export async function saveSettings(settings: Partial<AppSettings>) {
  return request<{ success: boolean }>("/settings", {
    method: "PUT",
    body: JSON.stringify(settings),
  });
}

export async function uploadResume(file: File) {
  return uploadFile<{ success: boolean; resume_path: string }>("/profile/resume", file);
}

// ── Gmail hiring outcomes ────────────────────────────────────────────────
export async function getGmailOutcomeStatus() {
  return request<GmailOutcomeStatus>("/gmail/outcomes/status");
}

export async function connectGmailOutcomes(
  clientId: string,
  clientSecret: string,
) {
  return request<{ success: boolean; authorization_url: string }>(
    "/gmail/outcomes/connect",
    {
      method: "POST",
      body: JSON.stringify({
        client_id: clientId,
        client_secret: clientSecret,
      }),
    },
  );
}

export async function syncGmailOutcomes() {
  return request<{
    success: boolean;
    scanned: number;
    updated: number;
    review_required: number;
    status: GmailOutcomeStatus;
  }>("/gmail/outcomes/sync", { method: "POST" });
}

export async function disconnectGmailOutcomes() {
  return request<{ success: boolean }>("/gmail/outcomes/disconnect", {
    method: "POST",
  });
}

export async function dismissGmailOutcomeReview(messageId: string) {
  return request<{ success: boolean }>("/gmail/outcomes/reviewed", {
    method: "POST",
    body: JSON.stringify({ message_id: messageId }),
  });
}

export async function openTrustedExternalUrl(
  url: string,
  target: "_blank" | "_self" = "_blank",
) {
  const parsed = new URL(url);
  if (
    parsed.protocol !== "https:"
    || !["accounts.google.com", "mail.google.com"].includes(parsed.hostname)
  ) {
    throw new Error("Hunter refused to open an unexpected external URL");
  }
  if (target === "_self") {
    window.location.assign(url);
    return;
  }
  const popup = window.open("about:blank", "_blank");
  if (!popup) {
    throw new Error("The browser blocked the new tab. Allow popups for Hunter and try again.");
  }
  popup.opener = null;
  popup.location.replace(url);
}

// ── Jobs ──────────────────────────────────────────────────────────────────
export async function getJobs(params?: { status?: string; screening_status?: string; category?: JobCategory; search?: string; limit?: number }) {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.screening_status) qs.set("screening_status", params.screening_status);
  if (params?.category) qs.set("category", params.category);
  if (params?.search) qs.set("search", params.search);
  if (params?.limit) qs.set("limit", String(params.limit));
  const query = qs.toString();
  return request<Job[]>(`/jobs${query ? `?${query}` : ""}`);
}

export async function getJobStats() {
  return request<JobStats>("/jobs/stats");
}

/** Fetch the jobs export as CSV text. Caller triggers the download. */
export async function exportJobsCsv(): Promise<string> {
  const res = await fetch(`${getBaseUrl()}/jobs/export`);
  if (!res.ok) {
    const text = await res.text().catch(() => "Unknown error");
    throw new Error(`API Error ${res.status}: ${text}`);
  }
  return res.text();
}

export async function startJobCollection(
  title?: string,
  maxJobs?: number,
  source?: string,
  filters?: Record<string, string>,
  searchUrl?: string,
) {
  const body: Record<string, unknown> = {};
  if (title) body.title = title;
  if (maxJobs) body.max_jobs = maxJobs;
  if (source) body.source = source;
  if (filters && Object.keys(filters).length > 0) body.filters = filters;
  if (searchUrl) body.search_url = searchUrl;
  return request<{ success: boolean; message: string }>("/jobs/collect", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateJobStatus(url: string, status: string) {
  return request<{ success: boolean }>("/jobs/status", {
    method: "PUT",
    body: JSON.stringify({ url, status }),
  });
}

export async function updateJobCategory(url: string, category: JobCategory) {
  return request<{ success: boolean; job: Job }>("/jobs/category", {
    method: "PUT",
    body: JSON.stringify({ url, category }),
  });
}

export async function updateJobCategories(urls: string[], category: JobCategory) {
  return request<{ success: boolean; updated: number; missing: string[] }>(
    "/jobs/categories",
    {
      method: "PUT",
      body: JSON.stringify({ urls, category }),
    },
  );
}

export async function updateApplicationStatus(
  url: string,
  status: ApplicationStatus,
) {
  return request<{ success: boolean; status: ApplicationStatus }>(
    "/jobs/application-status",
    {
      method: "PUT",
      body: JSON.stringify({ url, status }),
    },
  );
}

export async function resolveApplicationOutcome(
  url: string,
  attemptId: string,
  decision: ApplicationOutcomeDecision,
) {
  return request<{ success: boolean; decision: ApplicationOutcomeDecision; job: Job }>(
    "/jobs/application-outcome/resolve",
    {
      method: "POST",
      body: JSON.stringify({ url, attempt_id: attemptId, decision }),
    },
  );
}

export async function updateScreeningOverride(
  url: string,
  override: "qualified" | null,
) {
  return request<{
    success: boolean;
    status: "qualified" | "review" | "rejected";
    override: "qualified" | null;
  }>("/jobs/screening-override", {
    method: "PUT",
    body: JSON.stringify({ url, override }),
  });
}

export async function addJob(url: string, title?: string, company?: string, source?: string) {
  return request<{ success: boolean }>("/jobs/add", {
    method: "POST",
    body: JSON.stringify({ url, title, company, source }),
  });
}

export async function importLinkedInJob(url: string) {
  return request<{
    success: boolean;
    url: string;
    job: Job;
    classification_queued: boolean;
  }>("/jobs/import/linkedin", {
    method: "POST",
    body: JSON.stringify({ url }),
  });
}

export async function deleteJobs(urls: string[]) {
  return request<{ success: boolean; deleted: number }>("/jobs", {
    method: "DELETE",
    body: JSON.stringify({ urls }),
  });
}

export async function stopJobCollection() {
  return request<{ success: boolean }>("/jobs/collect/stop", { method: "POST" });
}

export async function stopJobClassification() {
  return request<{ success: boolean }>("/jobs/classify/stop", { method: "POST" });
}

export async function getCollectionStatus() {
  return request<CollectionStatus>("/jobs/collect/status");
}

// ── Memory ────────────────────────────────────────────────────────────────
export async function getMemoryStats() {
  return request<MemoryStats>("/memory/stats");
}

export async function getMemoryDomains() {
  return request<DomainInfo[]>("/memory/domains");
}

export async function getMemoriesForDomain(domain: string) {
  return request<Memory[]>(
    `/memory/domain/${encodeURIComponent(domain)}`
  );
}

export async function searchMemories(query: string) {
  return request<Memory[]>(`/memory/search?q=${encodeURIComponent(query)}`);
}

export async function decayMemories(days = 30) {
  return request<{ success: boolean; affected: number }>("/memory/decay", {
    method: "POST",
    body: JSON.stringify({ days }),
  });
}

export async function cleanupMemories() {
  return request<{ success: boolean; deleted: number }>("/memory/cleanup", {
    method: "POST",
  });
}

export async function exportMemories() {
  return request<Memory[]>("/memory/export");
}

export async function rerankCriticalMemories() {
  return request<{
    success: boolean;
    selected: number;
    scopes: number;
    estimated_tokens: number;
    model: string;
  }>("/memory/rerank-critical", { method: "POST" });
}

export async function getCriticalMemorySettings() {
  return request<CriticalMemorySettings>("/memory/critical-settings");
}

export async function updateCriticalMemorySettings(settings: CriticalMemorySettings) {
  return request<CriticalMemorySettings & { success: boolean; selected: number }>(
    "/memory/critical-settings",
    {
      method: "PUT",
      body: JSON.stringify(settings),
    }
  );
}

// ── Apply ─────────────────────────────────────────────────────────────────
export async function startApplying(params: {
  workers?: number;
  mode?: string;
  limit?: number;
  max_steps?: number;
  job_url?: string;
  job_urls?: string[];
  linkedin_outreach_enabled?: boolean;
}) {
  return request<{ success: boolean; message: string }>("/apply/start", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

export async function stopApplying() {
  return request<{ success: boolean }>("/apply/stop", { method: "POST" });
}

export async function getApplyStatus() {
  return request<ApplyStatus>("/apply/status");
}

// ── Autonomous mode ─────────────────────────────────────────────────────
export async function getAutomation() {
  return request<AutomationResponse>("/automation");
}

export async function saveAutomationConfig(config: Omit<AutomationConfig, "enabled">) {
  return request<{ success: boolean; config: AutomationConfig }>("/automation/config", {
    method: "PUT",
    body: JSON.stringify(config),
  });
}

export async function startAutomation() {
  return request<{ success: boolean } & AutomationResponse>("/automation/start", {
    method: "POST",
  });
}

export async function stopAutomation() {
  return request<{ success: boolean } & AutomationResponse>("/automation/stop", {
    method: "POST",
  });
}

// ── Dashboard / Metrics ───────────────────────────────────────────────────
export async function getDashboardData() {
  return request<DashboardResponse>("/dashboard");
}

export async function getMetricRuns(limit = 50) {
  return request<RunMetric[]>(`/metrics/runs?limit=${limit}`);
}

export async function getApplicationAttempt(attemptId: string) {
  return request<ApplicationAttemptDossier>(
    `/application-attempts/${encodeURIComponent(attemptId)}`,
  );
}

export async function getApplicationAttemptFile(attemptId: string, path: string) {
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  return requestBlob(
    `/application-attempts/${encodeURIComponent(attemptId)}/files/${encodedPath}`,
  );
}

export async function getApplicationAttemptStorage(videoRetentionDays = 14, dossierRetentionDays = 30) {
  const params = new URLSearchParams({
    video_retention_days: String(videoRetentionDays),
    dossier_retention_days: String(dossierRetentionDays),
  });
  return request<ApplicationAttemptStorage>(`/application-attempts/storage?${params}`);
}

export async function cleanupApplicationAttempts(videoRetentionDays = 14, dossierRetentionDays = 30) {
  return request<ApplicationAttemptCleanupResult>(
    "/application-attempts/retention/cleanup",
    {
      method: "POST",
      body: JSON.stringify({
        video_retention_days: videoRetentionDays,
        dossier_retention_days: dossierRetentionDays,
      }),
    },
  );
}

// ── Logs ─────────────────────────────────────────────────────────────────
export async function getRunLogs(runId: string, limit = 500) {
  return request<RunLog[]>(`/logs/runs/${encodeURIComponent(runId)}?limit=${limit}`);
}

export async function getRecentLogs(limit = 100) {
  return request<RunLog[]>(`/logs/recent?limit=${limit}`);
}

export async function getRunsWithLogs(limit = 50) {
  return request<RunWithLogs[]>(`/logs/runs?limit=${limit}`);
}

// ── Auth / Login Sessions ─────────────────────────────────────────────────
export async function getAuthStatus() {
  return request<{
    linkedin: { logged_in: boolean };
    gmail: { logged_in: boolean };
  }>("/auth/status");
}

export async function launchLogin(service: "linkedin" | "gmail") {
  return request<{ success: boolean; message: string }>(`/auth/login/${service}`, {
    method: "POST",
  });
}

export async function getManualBrowserStatus() {
  return request<ManualBrowserStatus>("/browser/manual/status");
}

export async function startManualBrowser() {
  return request<ManualBrowserStatus & { success: boolean }>("/browser/manual/start", {
    method: "POST",
  });
}

export async function stopManualBrowser() {
  return request<ManualBrowserStatus & { success: boolean }>("/browser/manual/stop", {
    method: "POST",
  });
}

// ── Setup / Onboarding ────────────────────────────────────────────────────
export interface SetupStatus {
  profile: boolean;
  llm: boolean;
  resume: boolean;
  chromium: boolean;
  linkedin: boolean;
  gmail: boolean;
  onboarding_completed: boolean;
  all_required_done: boolean;
}

export async function getSetupStatus() {
  return request<SetupStatus>("/setup/status");
}

export async function completeOnboarding() {
  return request<{ success: boolean }>("/setup/complete-onboarding", {
    method: "POST",
  });
}

export async function parseResumeToProfile() {
  return request<{
    success: boolean;
    message: string;
    profile?: Record<string, unknown>;
    fields_filled?: number;
  }>("/profile/parse-resume", { method: "POST" });
}

// ── Q&A Repository ────────────────────────────────────────────────────────
export async function getQAList(params?: { search?: string; folder?: "all" | "reviewed" | "to_review" | "unanswered"; unanswered?: boolean }) {
  const qs = new URLSearchParams();
  if (params?.search) qs.set("search", params.search);
  if (params?.folder && params.folder !== "all") qs.set("folder", params.folder);
  if (params?.unanswered) qs.set("unanswered", "true");
  const query = qs.toString();
  return request<QAEntry[]>(`/qa${query ? `?${query}` : ""}`);
}

export async function getQAStats() {
  return request<QAStats>("/qa/stats");
}

export async function updateQA(id: number, answer: string) {
  return request<{ success: boolean }>(`/qa/${id}`, {
    method: "PUT",
    body: JSON.stringify({ answer }),
  });
}

export async function updateQAReviewed(id: number, reviewed: boolean) {
  return request<{ success: boolean }>(`/qa/${id}`, {
    method: "PUT",
    body: JSON.stringify({ reviewed }),
  });
}

export async function deleteQA(id: number) {
  return request<{ success: boolean }>(`/qa/${id}`, { method: "DELETE" });
}

export async function mergeQA(sourceId: number, targetId: number) {
  return request<{ success: boolean }>(`/qa/${sourceId}/merge/${targetId}`, { method: "POST" });
}

export async function autoSquashQA() {
  return request<{ success: boolean; merged: number }>("/qa/auto-squash", { method: "POST" });
}

export async function smartSquashQA() {
  return request<{ success: boolean; merged: number }>("/qa/smart-squash", { method: "POST" });
}

// ── Resume Tailoring ─────────────────────────────────────────────────────
export async function tailorResumes(jobUrls: string[]) {
  return request<{ success: boolean; results: import("./types").TailorResult[] }>("/resume/tailor", {
    method: "POST",
    body: JSON.stringify({ job_urls: jobUrls }),
  });
}

export async function refineTailoredResume(jobUrl: string, instruction: string) {
  return request<{ success: boolean; content: string; path: string }>("/resume/tailor/refine", {
    method: "POST",
    body: JSON.stringify({ job_url: jobUrl, instruction }),
  });
}

export async function getTailoredResumeContent(jobUrl: string) {
  return request<{ success: boolean; content: string; path: string | null }>("/resume/tailor/get-by-url", {
    method: "POST",
    body: JSON.stringify({ job_url: jobUrl }),
  });
}

export async function openTailoredResumePdf(jobUrl: string) {
  const url = `${getBaseUrl()}/resume/tailor/pdf-by-url?job_url=${encodeURIComponent(jobUrl)}`;
  window.open(url, "_blank", "noopener,noreferrer");
}

// ── Cover Letter ─────────────────────────────────────────────────────────
export async function openCoverLetterFile(jobUrl: string) {
  const url = `${getBaseUrl()}/cover-letter/file-by-url?job_url=${encodeURIComponent(jobUrl)}`;
  window.open(url, "_blank", "noopener,noreferrer");
}

export async function generateCoverLetter(
  jobDescription: string,
  jobTitle: string,
  company: string,
  jobUrl?: string,
) {
  return request<{
    success: boolean;
    cover_letter: string;
    cover_letter_path: string;
    cover_letter_pdf_path: string;
  }>("/cover-letter/generate", {
    method: "POST",
    body: JSON.stringify({
      job_description: jobDescription,
      job_title: jobTitle,
      company,
      job_url: jobUrl,
    }),
  });
}

// ── Countries ────────────────────────────────────────────────────────────
export async function getCountries() {
  return request<{ success: boolean; countries: Record<string, import("./types").CountryConfig>; notice_period_options: string[] }>("/countries");
}

export async function getCountryConfig(code: string) {
  return request<{ success: boolean; config: import("./types").CountryConfig }>(`/countries/${code}`);
}

// ── Plugins ──────────────────────────────────────────────────────────────
export async function getPlugins(country?: string) {
  const params = country ? `?country=${country}` : "";
  return request<{ success: boolean; plugins: import("./types").PluginConfig[] }>(`/plugins${params}`);
}

export async function importPlugin(file: File) {
  return uploadFile<{ success: boolean; plugin: { name: string; display_name: string } }>(
    "/plugins/import",
    file,
  );
}

export async function togglePlugin(name: string, enabled: boolean) {
  return request<{ success: boolean }>(`/plugins/${name}/toggle`, {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  });
}

export async function removePlugin(name: string) {
  return request<{ success: boolean }>(`/plugins/${name}`, { method: "DELETE" });
}

export async function reloadPlugins() {
  return request<{ success: boolean; count: number }>("/plugins/reload", { method: "POST" });
}
