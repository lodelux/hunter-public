import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SettingsPage from "./Settings";
import {
  getSettings,
  saveSettings,
  getPlugins,
  togglePlugin,
  removePlugin,
  importPlugin,
  uploadResume,
  getApplicationAttemptStorage,
  cleanupApplicationAttempts,
  connectGmailOutcomes,
  disconnectGmailOutcomes,
  getGmailOutcomeStatus,
  getManualBrowserStatus,
  openTrustedExternalUrl,
  startManualBrowser,
  stopManualBrowser,
  syncGmailOutcomes,
} from "../lib/api";

// jsdom in this config does not provide a working localStorage; polyfill it.
function installLocalStorage() {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  });
}

// i18n: identity translator so assertions can use raw keys.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { changeLanguage: vi.fn() } }),
}));

// loadLanguage performs dynamic i18n resource loading — stub it out.
vi.mock("../i18n", () => ({
  loadLanguage: vi.fn(async () => {}),
}));

vi.mock("../lib/api", () => ({
  getSettings: vi.fn(),
  saveSettings: vi.fn(),
  getPlugins: vi.fn(),
  togglePlugin: vi.fn(),
  removePlugin: vi.fn(),
  importPlugin: vi.fn(),
  uploadResume: vi.fn(),
  getApplicationAttemptStorage: vi.fn(),
  cleanupApplicationAttempts: vi.fn(),
  connectGmailOutcomes: vi.fn(),
  disconnectGmailOutcomes: vi.fn(),
  getGmailOutcomeStatus: vi.fn(),
  getManualBrowserStatus: vi.fn(),
  openTrustedExternalUrl: vi.fn(),
  startManualBrowser: vi.fn(),
  stopManualBrowser: vi.fn(),
  syncGmailOutcomes: vi.fn(),
}));

const mockGetSettings = vi.mocked(getSettings);
const mockSaveSettings = vi.mocked(saveSettings);
const mockGetPlugins = vi.mocked(getPlugins);
const mockTogglePlugin = vi.mocked(togglePlugin);
const mockRemovePlugin = vi.mocked(removePlugin);
const mockImportPlugin = vi.mocked(importPlugin);
const mockUploadResume = vi.mocked(uploadResume);
const mockGetApplicationAttemptStorage = vi.mocked(getApplicationAttemptStorage);
const mockCleanupApplicationAttempts = vi.mocked(cleanupApplicationAttempts);
const mockConnectGmailOutcomes = vi.mocked(connectGmailOutcomes);
const mockDisconnectGmailOutcomes = vi.mocked(disconnectGmailOutcomes);
const mockGetGmailOutcomeStatus = vi.mocked(getGmailOutcomeStatus);
const mockGetManualBrowserStatus = vi.mocked(getManualBrowserStatus);
const mockOpenTrustedExternalUrl = vi.mocked(openTrustedExternalUrl);
const mockStartManualBrowser = vi.mocked(startManualBrowser);
const mockStopManualBrowser = vi.mocked(stopManualBrowser);
const mockSyncGmailOutcomes = vi.mocked(syncGmailOutcomes);
const scrollIntoViewMock = vi.fn();

const baseSettings = {
  resume_path: "/home/me/resume.pdf",
  blocked_domains: ["spam.com"],
  sensitive_data: { email: "me@example.com", password: "secret" },
  max_failures: 8,
  browser_headless: false,
  data_dir: "/data",
  stagger_delay: 5,
  dossier_retention_enabled: true,
  dossier_retention_days: 14,
  dossier_delete_after_days: 30,
  auto_reject_after_months: 1,
};

const builtinPlugin = {
  name: "linkedin",
  display_name: "LinkedIn",
  version: "1.0.0",
  author: "core",
  description: "LinkedIn jobs",
  countries: ["US", "GB"],
  website: "https://linkedin.com",
  requires_login: true,
  login_url: "https://linkedin.com/login",
  is_builtin: true,
  enabled: true,
  filters: [],
};

const communityPlugin = {
  ...builtinPlugin,
  name: "acme",
  display_name: "Acme Jobs",
  is_builtin: false,
  enabled: false,
};

const storageReport = {
  root: "/data/application_attempts",
  video_retention_days: 14,
  dossier_retention_days: 30,
  total_dossiers: 138,
  total_bytes: 1288490188,
  video_bytes: 1181116006,
  video_cleanup_dossiers: 40,
  video_cleanup_bytes: 536870912,
  dossier_cleanup_dossiers: 2,
  dossier_cleanup_bytes: 107374182,
  reclaimable_dossiers: 40,
  reclaimable_bytes: 644245094,
  protected_incomplete_dossiers: 3,
  candidates: [],
};

const gmailStatus = {
  client_configured: false,
  connected: false,
  enabled: false,
  account_email: null,
  last_success_at: null,
  last_error: null,
  last_sync_summary: null,
  review_required_count: 0,
  review_required: [],
  recent_events: [],
  telegram_configured: true,
};

const manualBrowserStatus = {
  state: "stopped" as const,
  message: null,
  started_at: null,
  supported: true,
  dependencies_ready: true,
  missing_dependencies: [],
  display: ":99",
  vnc_port: 5901,
};

describe("SettingsPage", () => {
  beforeEach(() => {
    window.location.hash = "";
    scrollIntoViewMock.mockReset();
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoViewMock,
    });
    installLocalStorage();
    mockGetSettings.mockResolvedValue(structuredClone(baseSettings) as never);
    mockSaveSettings.mockResolvedValue({ success: true } as never);
    mockGetPlugins.mockResolvedValue({
      success: true,
      plugins: [structuredClone(builtinPlugin), structuredClone(communityPlugin)],
    } as never);
    mockTogglePlugin.mockResolvedValue({ success: true } as never);
    mockRemovePlugin.mockResolvedValue({ success: true } as never);
    mockImportPlugin.mockResolvedValue({
      success: true,
      plugin: { name: "new", display_name: "New" },
    } as never);
    mockUploadResume.mockResolvedValue({
      success: true,
      resume_path: "/data/resumes/resume.pdf",
    });
    mockGetApplicationAttemptStorage.mockResolvedValue(structuredClone(storageReport) as never);
    mockCleanupApplicationAttempts.mockResolvedValue({
      success: true,
      compacted_dossiers: 40,
      deleted_dossiers: 2,
      removed_bytes: 536870912,
      removed_files: 800,
      storage: {
        ...structuredClone(storageReport),
        total_bytes: 751619276,
        video_bytes: 644245094,
        reclaimable_dossiers: 0,
        reclaimable_bytes: 0,
      },
    } as never);
    mockGetGmailOutcomeStatus.mockResolvedValue(structuredClone(gmailStatus) as never);
    mockGetManualBrowserStatus.mockResolvedValue(structuredClone(manualBrowserStatus) as never);
    mockStartManualBrowser.mockResolvedValue({
      ...structuredClone(manualBrowserStatus),
      success: true,
      state: "starting",
      message: "Starting private virtual display and Chrome...",
    } as never);
    mockStopManualBrowser.mockResolvedValue({
      ...structuredClone(manualBrowserStatus),
      success: true,
      state: "stopping",
      message: "Stopping remote browser...",
    } as never);
    mockConnectGmailOutcomes.mockResolvedValue({
      success: true,
      authorization_url: "https://accounts.google.com/o/oauth2/auth?state=test",
    });
    mockDisconnectGmailOutcomes.mockResolvedValue({ success: true });
    mockOpenTrustedExternalUrl.mockResolvedValue(undefined);
    mockSyncGmailOutcomes.mockResolvedValue({
      success: true,
      scanned: 2,
      updated: 1,
      review_required: 0,
      status: {
        ...structuredClone(gmailStatus),
        client_configured: true,
        connected: true,
        enabled: true,
        account_email: "candidate@example.com",
      },
    } as never);
  });

  it("does not show autosave status before settings load", async () => {
    render(<SettingsPage />);
    expect(screen.queryByText("Changes save automatically")).not.toBeInTheDocument();
    expect(await screen.findByText("Changes save automatically")).toBeInTheDocument();
  });

  it("loads and renders the settings values", async () => {
    render(<SettingsPage />);
    expect(await screen.findByDisplayValue("/home/me/resume.pdf")).toBeInTheDocument();
    expect(screen.getByDisplayValue("me@example.com")).toBeInTheDocument();
    expect(screen.getByDisplayValue("secret")).toBeInTheDocument();
    expect(screen.getByDisplayValue(8)).toBeInTheDocument();
    // Blocked domain tag renders.
    expect(screen.getByText("spam.com")).toBeInTheDocument();
    expect(screen.getByText("Connect Gmail with read-only access")).toBeInTheDocument();
    expect(screen.getByText("Remote manual browser")).toBeInTheDocument();
  });

  it("starts the private remote browser from agent settings", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByText("Remote manual browser");

    await user.click(screen.getByRole("button", { name: "Start browser" }));

    await waitFor(() => expect(mockStartManualBrowser).toHaveBeenCalled());
    expect(await screen.findByText("Starting private virtual display and Chrome...")).toBeInTheDocument();
  });

  it("explains how to install missing remote browser dependencies", async () => {
    mockGetManualBrowserStatus.mockResolvedValue({
      ...structuredClone(manualBrowserStatus),
      dependencies_ready: false,
      missing_dependencies: ["Xvfb", "x11vnc"],
    } as never);

    render(<SettingsPage />);

    expect(await screen.findByText(/Missing Xvfb and x11vnc/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start browser" })).toBeDisabled();
  });

  it("scrolls directly to Gmail settings when deep-linked", async () => {
    window.location.hash = "#/settings?section=gmail";
    render(<SettingsPage />);

    await screen.findByText("Connect Gmail with read-only access");
    await waitFor(() => expect(scrollIntoViewMock).toHaveBeenCalledWith({ block: "start" }));
  });

  it("starts read-only Gmail OAuth with the supplied desktop client", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByText("Connect Gmail with read-only access");

    await user.type(screen.getByLabelText("Google OAuth client ID"), "client-id");
    await user.type(screen.getByLabelText("Google OAuth client secret"), "client-secret");
    await user.click(screen.getByRole("button", { name: "Connect Gmail" }));

    await waitFor(() => expect(mockConnectGmailOutcomes).toHaveBeenCalledWith("client-id", "client-secret"));
    expect(mockOpenTrustedExternalUrl).toHaveBeenCalledWith(
      "https://accounts.google.com/o/oauth2/auth?state=test",
      "_self",
    );
    expect(await screen.findByText(/Complete the read-only Gmail authorization/)).toBeInTheDocument();
  });

  it("syncs connected Gmail and reports the result", async () => {
    mockGetGmailOutcomeStatus.mockResolvedValue({
      ...structuredClone(gmailStatus),
      client_configured: true,
      connected: true,
      enabled: true,
      account_email: "candidate@example.com",
    } as never);
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByText("Connected as candidate@example.com");
    expect(screen.getByText("Pending reviews")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Sync now" }));

    await waitFor(() => expect(mockSyncGmailOutcomes).toHaveBeenCalled());
    expect(await screen.findByText("Scanned 2 new emails, updated 1, and flagged 0 for review.")).toBeInTheDocument();
  });

  it("links Gmail manual-review work to the independent email panel", async () => {
    const reviewStatus = {
      ...structuredClone(gmailStatus),
      client_configured: true,
      connected: true,
      enabled: true,
      review_required_count: 1,
      review_required: [{
        message_id: "message-1",
        thread_id: "thread-1",
        received_at: "2026-08-20T12:00:00Z",
        subject: "Your application",
        sender: "Acme Recruiting",
        reason: "The outcome is ambiguous.",
        gmail_url: "https://mail.google.com/mail/u/0/#all/thread-1",
        job_url: "https://jobs.example/acme",
        company: "Acme",
        title: "Engineer",
      }],
    };
    mockGetGmailOutcomeStatus.mockResolvedValue(reviewStatus as never);
    render(<SettingsPage />);
    expect(await screen.findByText("1 email outcome awaiting review")).toBeInTheDocument();
    expect(screen.queryByText("Your application")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Review emails/ })).toHaveAttribute(
      "href",
      "#/jobs?tab=pending&category=rejected&review=email",
    );
  });

  it("renders plugins, builtin badge, and the community remove button", async () => {
    render(<SettingsPage />);
    expect(await screen.findByText("LinkedIn")).toBeInTheDocument();
    expect(screen.getByText("Acme Jobs")).toBeInTheDocument();
    expect(screen.getByText("Built-in")).toBeInTheDocument();
  });

  it("shows dossier storage and previews the retention policy", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);

    expect(await screen.findByText("1.20 GB")).toBeInTheDocument();
    expect(screen.getByText("1.10 GB")).toBeInTheDocument();
    expect(screen.getByText("614.4 MB")).toBeInTheDocument();
    expect(screen.getByText(/3 active or unreadable dossiers are protected/)).toBeInTheDocument();
    expect(screen.getByText(/permanently delete 2 full dossiers/)).toBeInTheDocument();

    const days = screen.getByLabelText(/Remove video after/);
    await user.clear(days);
    await user.type(days, "60");
    await user.click(screen.getByRole("button", { name: "Preview cleanup" }));

    await waitFor(() => expect(mockGetApplicationAttemptStorage).toHaveBeenLastCalledWith(60, 60));
  });

  it("confirms and runs the two-stage cleanup", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByText("1.20 GB");

    await user.click(screen.getByRole("button", { name: "Run cleanup now" }));

    await waitFor(() => expect(mockCleanupApplicationAttempts).toHaveBeenCalledWith(14, 30));
    expect(await screen.findByText("Reclaimed 512.0 MB: removed 40 videos and deleted 2 full dossiers.")).toBeInTheDocument();
  });

  it("autosaves settings with the current field values", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByDisplayValue("/home/me/resume.pdf");

    const resumePath = screen.getByDisplayValue("/home/me/resume.pdf");
    await user.clear(resumePath);
    await user.type(resumePath, "/home/me/new-resume.pdf");

    await waitFor(() =>
      expect(mockSaveSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          resume_path: "/home/me/new-resume.pdf",
          blocked_domains: ["spam.com"],
          sensitive_data: { email: "me@example.com", password: "secret" },
          max_failures: 8,
          browser_headless: false,
          auto_reject_after_months: 1,
          dossier_retention_enabled: true,
          dossier_retention_days: 14,
          dossier_delete_after_days: 30,
        }),
      ),
    );
    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("shows an error banner when saving fails", async () => {
    mockSaveSettings.mockRejectedValue(new Error("boom"));
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByDisplayValue("/home/me/resume.pdf");

    await user.type(screen.getByDisplayValue("/home/me/resume.pdf"), "-changed");
    expect(await screen.findByText("Failed to save settings. Please try again.")).toBeInTheDocument();
  });

  it("selects headless browser mode and autosaves it", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByDisplayValue("/home/me/resume.pdf");

    const headless = screen.getByRole("radio", { name: "Headless" });
    expect(screen.getByRole("radio", { name: "Headed" })).toHaveAttribute("aria-checked", "true");
    await user.click(headless);
    expect(headless).toHaveAttribute("aria-checked", "true");

    await waitFor(() =>
      expect(mockSaveSettings).toHaveBeenCalledWith(
        expect.objectContaining({ browser_headless: true }),
      ),
    );
  });

  it("toggles a plugin on/off via the API", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByText("Acme Jobs");

    // The community plugin starts disabled (unchecked); toggle it on.
    const checkboxes = screen.getAllByRole("checkbox");
    const acmeToggle = checkboxes.find((c) => !(c as HTMLInputElement).checked);
    await user.click(acmeToggle!);
    await waitFor(() => expect(mockTogglePlugin).toHaveBeenCalledWith("acme", true));
  });

  it("removes a community plugin", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByText("Acme Jobs");

    // Only the community plugin has a remove (Trash) button.
    const removeButtons = screen
      .getAllByRole("button")
      .filter((b) => b.querySelector("svg.lucide-trash2") || b.className.includes("text-destructive"));
    await user.click(removeButtons[0]);
    await waitFor(() => expect(mockRemovePlugin).toHaveBeenCalledWith("acme"));
    await waitFor(() => expect(screen.queryByText("Acme Jobs")).not.toBeInTheDocument());
  });

  it("adds a blocked domain via the tag input", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByDisplayValue("/home/me/resume.pdf");

    const tagInput = screen.getByPlaceholderText("example.com");
    await user.type(tagInput, "blocked.io{Enter}");
    expect(await screen.findByText("blocked.io")).toBeInTheDocument();
  });

  it("imports a plugin when a file is chosen", async () => {
    const user = userEvent.setup();
    const { container } = render(<SettingsPage />);
    await screen.findByText("Acme Jobs");

    const file = new File(["name: new"], "plugin.yaml", { type: "application/yaml" });
    const input = container.querySelector<HTMLInputElement>('input[type="file"][accept*="yaml"]');
    expect(input).not.toBeNull();
    await user.upload(input!, file);

    await waitFor(() => expect(mockImportPlugin).toHaveBeenCalledWith(file));
    // Plugins reload after import.
    await waitFor(() => expect(mockGetPlugins).toHaveBeenCalledTimes(2));
  });

  it("uploads a resume file and updates the host path", async () => {
    const user = userEvent.setup();
    const { container } = render(<SettingsPage />);
    await screen.findByDisplayValue("/home/me/resume.pdf");

    const file = new File(["%PDF-1.4"], "resume.pdf", { type: "application/pdf" });
    const input = container.querySelector<HTMLInputElement>('input[type="file"][accept*="pdf"]');
    expect(input).not.toBeNull();
    await user.upload(input!, file);

    await waitFor(() => expect(mockUploadResume).toHaveBeenCalledWith(file));
    expect(await screen.findByDisplayValue("/data/resumes/resume.pdf")).toBeInTheDocument();
  });

  it("renders the theme toggle and persists a selection (#41)", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByDisplayValue("/home/me/resume.pdf");

    const dark = screen.getByRole("radio", { name: /appearance\.dark/ });
    expect(screen.getByRole("radio", { name: /appearance\.light/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /appearance\.system/ })).toBeInTheDocument();

    await user.click(dark);
    // Applies immediately and mirrors to the backend.
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    await waitFor(() => expect(mockSaveSettings).toHaveBeenCalledWith(
      expect.objectContaining({ theme: "dark" }),
    ));
    expect(dark).toHaveAttribute("aria-checked", "true");

    // Clean up the global <html> class so other tests aren't affected.
    document.documentElement.classList.remove("dark");
  });

  it("applies a theme loaded from backend settings (#41)", async () => {
    mockGetSettings.mockResolvedValue({ ...baseSettings, theme: "dark" } as never);
    render(<SettingsPage />);
    await screen.findByDisplayValue("/home/me/resume.pdf");
    await waitFor(() => expect(document.documentElement.classList.contains("dark")).toBe(true));
    document.documentElement.classList.remove("dark");
  });
});
