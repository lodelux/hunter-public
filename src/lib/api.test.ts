/**
 * Tests for src/lib/api.ts — the FastAPI backend HTTP client.
 *
 * Strategy:
 *  - Global `fetch` is stubbed (vi.fn) so NO real network occurs.
 *  - api.ts reads its optional Vite base URL at module load, so tests use a
 *    fresh dynamic import when changing that environment value.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

type ApiModule = typeof import("./api");

const DEFAULT_BASE = "";

let fetchMock: ReturnType<typeof vi.fn>;

/** Build a Response-like fetch result. */
function jsonResponse(body: unknown, status = 200): Response {
  const text = body === undefined ? "" : JSON.stringify(body);
  return new Response(text, {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Re-import a fresh api module so module-scope state is reset per test. */
async function loadApi(): Promise<ApiModule> {
  vi.resetModules();
  return import("./api");
}

beforeEach(() => {
  fetchMock = vi.fn(async () => jsonResponse({ ok: true }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("api — base URL", () => {
  it("defaults to same-origin routes", async () => {
    const api = await loadApi();
    await api.checkHealth();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/health");
  });

  it("uses VITE_API_BASE_URL when configured", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "/api");
    const api = await loadApi();
    await api.getProfile();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/profile");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });
});

describe("api — response handling", () => {
  it("parses JSON response bodies", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValueOnce(jsonResponse({ status: "ok", uptime: 5 }));
    const res = await api.checkHealth();
    expect(res).toEqual({ status: "ok", uptime: 5 });
  });

  it("returns an empty object for an empty (no body) response", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValueOnce(new Response("", { status: 200 }));
    const res = await api.completeOnboarding();
    expect(res).toEqual({});
  });

  it("throws on a non-OK response including status and body text", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValueOnce(
      new Response("boom details", { status: 500 })
    );
    await expect(api.getProfile()).rejects.toThrow(
      "API Error 500: boom details"
    );
  });

  it("throws an 'Invalid JSON' error when the body is not valid JSON", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValueOnce(
      new Response("not-json-at-all", { status: 200 })
    );
    await expect(api.getProfile()).rejects.toThrow(
      "Invalid JSON response from /profile"
    );
  });
});

describe("api — endpoint method/path/body", () => {
  beforeEach(() => {
    // Default OK json for all endpoint shape tests.
    fetchMock.mockResolvedValue(jsonResponse({ success: true }));
  });

  /** Helper: read the (url, init) of the single fetch call. */
  function lastCall() {
    const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit];
    return { url, init, body: init.body ? JSON.parse(init.body as string) : undefined };
  }

  it("getLLMSettings -> GET /settings/llm", async () => {
    const api = await loadApi();
    await api.getLLMSettings();
    const { url, init } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/settings/llm`);
    expect(init.method).toBeUndefined(); // GET (no method specified)
  });

  it("saveLLMSettings -> PUT /settings/llm with JSON body", async () => {
    const api = await loadApi();
    const settings = { provider: "ollama", model: "llama3" } as never;
    await api.saveLLMSettings(settings);
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/settings/llm`);
    expect(init.method).toBe("PUT");
    expect(body).toEqual({ provider: "ollama", model: "llama3" });
  });

  it("testLLMConnection -> POST /llm/test with settings body", async () => {
    const api = await loadApi();
    const settings = { provider: "openai" } as never;
    await api.testLLMConnection(settings);
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/llm/test`);
    expect(init.method).toBe("POST");
    expect(body).toEqual({ provider: "openai" });
  });

  it("fetchOllamaModels -> POST /llm/ollama-models with base_url", async () => {
    const api = await loadApi();
    await api.fetchOllamaModels("http://localhost:11434");
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/llm/ollama-models`);
    expect(init.method).toBe("POST");
    expect(body).toEqual({ base_url: "http://localhost:11434" });
  });

  it("saveProfile -> PUT /profile with profile body", async () => {
    const api = await loadApi();
    const profile = { full_name: "Ada" } as never;
    await api.saveProfile(profile);
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/profile`);
    expect(init.method).toBe("PUT");
    expect(body).toEqual({ full_name: "Ada" });
  });

  it("uploadResume sends the PDF bytes and encoded filename", async () => {
    const api = await loadApi();
    const file = new File(["%PDF-1.4"], "Ada CV.pdf", { type: "application/pdf" });

    await api.uploadResume(file);

    const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit];
    expect(url).toBe("/profile/resume");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(file);
    expect(init.headers).toEqual({
      "Content-Type": "application/pdf",
      "X-Hunter-Filename": "Ada%20CV.pdf",
    });
  });

  it("importPlugin sends YAML bytes through the upload endpoint", async () => {
    const api = await loadApi();
    const file = new File(["name: acme"], "acme.yaml", { type: "application/yaml" });

    await api.importPlugin(file);

    const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit];
    expect(url).toBe("/plugins/import");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(file);
    expect(init.headers).toEqual({
      "Content-Type": "application/yaml",
      "X-Hunter-Filename": "acme.yaml",
    });
  });

  it("updateJobStatus -> PUT /jobs/status with url+status", async () => {
    const api = await loadApi();
    await api.updateJobStatus("https://job/1", "applied");
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/jobs/status`);
    expect(init.method).toBe("PUT");
    expect(body).toEqual({ url: "https://job/1", status: "applied" });
  });

  it("updateApplicationStatus -> PUT /jobs/application-status", async () => {
    const api = await loadApi();
    await api.updateApplicationStatus("https://job/1", "interview");
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/jobs/application-status`);
    expect(init.method).toBe("PUT");
    expect(body).toEqual({ url: "https://job/1", status: "interview" });
  });

  it("updateJobCategory -> PUT /jobs/category", async () => {
    const api = await loadApi();
    await api.updateJobCategory("https://job/1", "qualified");
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/jobs/category`);
    expect(init.method).toBe("PUT");
    expect(body).toEqual({ url: "https://job/1", category: "qualified" });
  });

  it("updateScreeningOverride -> PUT /jobs/screening-override", async () => {
    const api = await loadApi();
    await api.updateScreeningOverride("https://job/1", "qualified");
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/jobs/screening-override`);
    expect(init.method).toBe("PUT");
    expect(body).toEqual({
      url: "https://job/1",
      override: "qualified",
    });
  });

  it("deleteJobs -> DELETE /jobs with urls body", async () => {
    const api = await loadApi();
    await api.deleteJobs(["a", "b"]);
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/jobs`);
    expect(init.method).toBe("DELETE");
    expect(body).toEqual({ urls: ["a", "b"] });
  });

  it("stopJobCollection -> POST /jobs/collect/stop with no body", async () => {
    const api = await loadApi();
    await api.stopJobCollection();
    const { url, init } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/jobs/collect/stop`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("stopJobClassification -> POST /jobs/classify/stop with no body", async () => {
    const api = await loadApi();
    await api.stopJobClassification();
    const { url, init } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/jobs/classify/stop`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("stopApplying -> POST /apply/stop with no body", async () => {
    const api = await loadApi();
    await api.stopApplying();
    const { url, init } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/apply/stop`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("updateQA -> PUT /qa/:id with answer body", async () => {
    const api = await loadApi();
    await api.updateQA(42, "my answer");
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/qa/42`);
    expect(init.method).toBe("PUT");
    expect(body).toEqual({ answer: "my answer" });
  });

  it("updateQAReviewed -> PUT /qa/:id with reviewed body", async () => {
    const api = await loadApi();
    await api.updateQAReviewed(42, true);
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/qa/42`);
    expect(init.method).toBe("PUT");
    expect(body).toEqual({ reviewed: true });
  });

  it("deleteQA -> DELETE /qa/:id", async () => {
    const api = await loadApi();
    await api.deleteQA(7);
    const { url, init } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/qa/7`);
    expect(init.method).toBe("DELETE");
  });

  it("mergeQA -> POST /qa/:source/merge/:target", async () => {
    const api = await loadApi();
    await api.mergeQA(1, 2);
    const { url, init } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/qa/1/merge/2`);
    expect(init.method).toBe("POST");
  });

  it("launchLogin -> POST /auth/login/:service", async () => {
    const api = await loadApi();
    await api.launchLogin("linkedin");
    const { url, init } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/auth/login/linkedin`);
    expect(init.method).toBe("POST");
  });

  it("getApplicationAttempt -> GET /application-attempts/:id", async () => {
    const api = await loadApi();
    await api.getApplicationAttempt("attempt/with spaces");
    const { url, init } = lastCall();
    expect(url).toBe(
      `${DEFAULT_BASE}/application-attempts/attempt%2Fwith%20spaces`,
    );
    expect(init.method).toBeUndefined();
  });

  it("getApplicationAttemptFile fetches a binary artifact", async () => {
    fetchMock.mockResolvedValueOnce(new Response("image-data", {
      status: 200,
      headers: { "Content-Type": "image/png" },
    }));
    const api = await loadApi();

    const blob = await api.getApplicationAttemptFile(
      "attempt-123",
      "screenshots/final image.png",
    );

    const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit | undefined];
    expect(url).toBe(
      `${DEFAULT_BASE}/application-attempts/attempt-123/files/screenshots/final%20image.png`,
    );
    expect(init).toBeUndefined();
    expect(await blob.text()).toBe("image-data");
  });

  it("togglePlugin -> PUT /plugins/:name/toggle with enabled flag", async () => {
    const api = await loadApi();
    await api.togglePlugin("greenhouse", true);
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/plugins/greenhouse/toggle`);
    expect(init.method).toBe("PUT");
    expect(body).toEqual({ enabled: true });
  });

  it("tailorResumes -> POST /resume/tailor with job_urls", async () => {
    const api = await loadApi();
    await api.tailorResumes(["u1"]);
    const { url, init, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/resume/tailor`);
    expect(init.method).toBe("POST");
    expect(body).toEqual({ job_urls: ["u1"] });
  });

  it("openTailoredResumePdf opens the inline PDF endpoint", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const api = await loadApi();

    await api.openTailoredResumePdf("https://job/1");

    expect(open).toHaveBeenCalledWith(
      `/resume/tailor/pdf-by-url?job_url=${encodeURIComponent("https://job/1")}`,
      "_blank",
      "noopener,noreferrer",
    );
  });

  it("openCoverLetterFile opens the inline PDF endpoint", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const api = await loadApi();

    await api.openCoverLetterFile("https://job/1");

    expect(open).toHaveBeenCalledWith(
      `/cover-letter/file-by-url?job_url=${encodeURIComponent("https://job/1")}`,
      "_blank",
      "noopener,noreferrer",
    );
  });

  it("generateCoverLetter -> POST /cover-letter/generate with mapped fields", async () => {
    const api = await loadApi();
    await api.generateCoverLetter("desc", "Engineer", "Acme", "https://job/1");
    const { url, body } = lastCall();
    expect(url).toBe(`${DEFAULT_BASE}/cover-letter/generate`);
    expect(body).toEqual({
      job_description: "desc",
      job_title: "Engineer",
      company: "Acme",
      job_url: "https://job/1",
    });
  });
});

describe("api — query-string builders", () => {
  beforeEach(() => {
    fetchMock.mockResolvedValue(jsonResponse([]));
  });

  it("getJobs with no params hits /jobs (no query string)", async () => {
    const api = await loadApi();
    await api.getJobs();
    expect(fetchMock.mock.calls.at(-1)![0]).toBe(`${DEFAULT_BASE}/jobs`);
  });

  it("getJobs builds a query string from provided params", async () => {
    const api = await loadApi();
    await api.getJobs({ status: "new", screening_status: "qualified", category: "review", search: "react dev", limit: 25 });
    const url = fetchMock.mock.calls.at(-1)![0] as string;
    expect(url.startsWith(`${DEFAULT_BASE}/jobs?`)).toBe(true);
    expect(url).toContain("status=new");
    expect(url).toContain("screening_status=qualified");
    expect(url).toContain("category=review");
    expect(url).toContain("search=react+dev");
    expect(url).toContain("limit=25");
  });

  it("getJobs omits falsy params (e.g. limit 0 / empty status)", async () => {
    const api = await loadApi();
    await api.getJobs({ status: "", search: "", limit: 0 });
    expect(fetchMock.mock.calls.at(-1)![0]).toBe(`${DEFAULT_BASE}/jobs`);
  });

  it("getQAList sets unanswered=true only when flagged", async () => {
    const api = await loadApi();
    await api.getQAList({ unanswered: true });
    const url = fetchMock.mock.calls.at(-1)![0] as string;
    expect(url).toContain("unanswered=true");
  });

  it("getQAList includes the selected Q&A folder", async () => {
    const api = await loadApi();
    await api.getQAList({ folder: "to_review" });
    const url = fetchMock.mock.calls.at(-1)![0] as string;
    expect(url).toContain("folder=to_review");
  });

  it("searchMemories url-encodes the query", async () => {
    const api = await loadApi();
    await api.searchMemories("c++ & rust");
    const url = fetchMock.mock.calls.at(-1)![0] as string;
    expect(url).toBe(
      `${DEFAULT_BASE}/memory/search?q=${encodeURIComponent("c++ & rust")}`
    );
  });

  it("getMemoriesForDomain encodes the domain path segment", async () => {
    const api = await loadApi();
    await api.getMemoriesForDomain("a/b c");
    const url = fetchMock.mock.calls.at(-1)![0] as string;
    expect(url).toBe(
      `${DEFAULT_BASE}/memory/domain/${encodeURIComponent("a/b c")}`
    );
  });

  it("rerankCriticalMemories posts to the critical ranking endpoint", async () => {
    const api = await loadApi();
    await api.rerankCriticalMemories();
    expect(fetchMock.mock.calls.at(-1)![0]).toBe(`${DEFAULT_BASE}/memory/rerank-critical`);
    expect((fetchMock.mock.calls.at(-1)![1] as RequestInit).method).toBe("POST");
  });

  it("gets critical memory settings", async () => {
    const api = await loadApi();
    await api.getCriticalMemorySettings();
    expect(fetchMock.mock.calls.at(-1)![0]).toBe(`${DEFAULT_BASE}/memory/critical-settings`);
  });

  it("updates critical memory settings", async () => {
    const api = await loadApi();
    await api.updateCriticalMemorySettings({ auto_rerank: false, max_count: 7, max_tokens: 900 });
    const [url, init] = fetchMock.mock.calls.at(-1)!;
    expect(url).toBe(`${DEFAULT_BASE}/memory/critical-settings`);
    expect((init as RequestInit).method).toBe("PUT");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      auto_rerank: false,
      max_count: 7,
      max_tokens: 900,
    });
  });

  it("getMetricRuns uses default limit of 50", async () => {
    const api = await loadApi();
    await api.getMetricRuns();
    expect(fetchMock.mock.calls.at(-1)![0]).toBe(
      `${DEFAULT_BASE}/metrics/runs?limit=50`
    );
  });

  it("getRunLogs encodes the run id and includes limit", async () => {
    const api = await loadApi();
    await api.getRunLogs("run 1/2", 10);
    const url = fetchMock.mock.calls.at(-1)![0] as string;
    expect(url).toBe(
      `${DEFAULT_BASE}/logs/runs/${encodeURIComponent("run 1/2")}?limit=10`
    );
  });
});

describe("api — startJobCollection body assembly", () => {
  beforeEach(() => {
    fetchMock.mockResolvedValue(jsonResponse({ success: true, message: "ok" }));
  });

  it("includes only provided fields", async () => {
    const api = await loadApi();
    await api.startJobCollection("Engineer", 20, "linkedin", { remote: "true" });
    const body = JSON.parse(
      (fetchMock.mock.calls.at(-1)![1] as RequestInit).body as string
    );
    expect(body).toEqual({
      title: "Engineer",
      max_jobs: 20,
      source: "linkedin",
      filters: { remote: "true" },
    });
  });

  it("omits empty filters object and undefined args", async () => {
    const api = await loadApi();
    await api.startJobCollection(undefined, undefined, undefined, {});
    const body = JSON.parse(
      (fetchMock.mock.calls.at(-1)![1] as RequestInit).body as string
    );
    expect(body).toEqual({});
  });

  it("includes a LinkedIn search URL", async () => {
    const api = await loadApi();
    await api.startJobCollection(
      undefined,
      10,
      "linkedin",
      undefined,
      "https://www.linkedin.com/jobs/search-results/?keywords=engineer",
    );
    const body = JSON.parse(
      (fetchMock.mock.calls.at(-1)![1] as RequestInit).body as string,
    );
    expect(body).toEqual({
      max_jobs: 10,
      source: "linkedin",
      search_url: "https://www.linkedin.com/jobs/search-results/?keywords=engineer",
    });
  });
});

describe("api — importLinkedInJob", () => {
  it("posts the URL to the dedicated LinkedIn import endpoint", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ success: true, url: "https://www.linkedin.com/jobs/view/123" }),
    );

    await api.importLinkedInJob("https://www.linkedin.com/jobs/view/123");

    expect(fetchMock.mock.calls.at(-1)![0]).toBe(
      `${DEFAULT_BASE}/jobs/import/linkedin`,
    );
    const body = JSON.parse(
      (fetchMock.mock.calls.at(-1)![1] as RequestInit).body as string,
    );
    expect(body).toEqual({ url: "https://www.linkedin.com/jobs/view/123" });
  });
});

describe("api — getChromiumStatus (bypasses request helper)", () => {
  it("calls /chromium/status without auth header and returns parsed json", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ state: "ready", message: "ok" })
    );
    const res = await api.getChromiumStatus();
    expect(res).toEqual({ state: "ready", message: "ok" });
    // This endpoint calls fetch directly with only the URL (no init object).
    expect(fetchMock.mock.calls.at(-1)![0]).toBe(`${DEFAULT_BASE}/chromium/status`);
    expect(fetchMock.mock.calls.at(-1)![1]).toBeUndefined();
  });
});
