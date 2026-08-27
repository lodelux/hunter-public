import { useCallback, useState, useEffect, useMemo, useRef } from "react";
import { CheckCircle, FolderOpen, X, Upload, Trash2, Sun, Moon, Monitor, Cpu, HardDrive, Loader2, RefreshCw, Mail, ArrowRight } from "lucide-react";
import {
  cleanupApplicationAttempts,
  connectGmailOutcomes,
  disconnectGmailOutcomes,
  getApplicationAttemptStorage,
  getGmailOutcomeStatus,
  getManualBrowserStatus,
  getSettings,
  openTrustedExternalUrl,
  saveSettings,
  startManualBrowser,
  stopManualBrowser,
  syncGmailOutcomes,
  getPlugins,
  togglePlugin,
  removePlugin,
  importPlugin,
  uploadResume,
} from "../lib/api";
import { getStoredTheme, setTheme, type ThemeMode } from "../lib/theme";
import { useTranslation } from "react-i18next";
import { PageHeader, LoadingSpinner, Section } from "../components/ui";
import TagInput from "../components/ui/TagInput";
import type { ApplicationAttemptStorage, GmailOutcomeStatus, ManualBrowserStatus, PluginConfig } from "../lib/types";
import { LANGUAGE_NAMES, getSavedLanguage, saveLanguagePreference } from "../i18n/languageDetection";
import { loadLanguage } from "../i18n";
import { useAutosave } from "../hooks/useAutosave";

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "Never";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Never" : date.toLocaleString();
}

function requestedSettingsSection(): string | null {
  const query = window.location.hash.split("?", 2)[1];
  return query ? new URLSearchParams(query).get("section") : null;
}

export default function SettingsPage() {
  const { t } = useTranslation("settings");
  const [resumePath, setResumePath] = useState("");
  const [blockedDomains, setBlockedDomains] = useState<string[]>([]);
  const [newDomain, setNewDomain] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [maxFailures, setMaxFailures] = useState(8);
  const [browserHeadless, setBrowserHeadless] = useState(false);
  const [autoRejectAfterMonths, setAutoRejectAfterMonths] = useState("1");
  const [theme, setThemeState] = useState<ThemeMode>(getStoredTheme());
  const [currentLanguage, setCurrentLanguage] = useState<string>(getSavedLanguage() || "");
  const [loading, setLoading] = useState(true);
  const [plugins, setPlugins] = useState<PluginConfig[]>([]);
  const [retentionEnabled, setRetentionEnabled] = useState(true);
  const [videoRetentionDays, setVideoRetentionDays] = useState("14");
  const [dossierRetentionDays, setDossierRetentionDays] = useState("30");
  const [storage, setStorage] = useState<ApplicationAttemptStorage | null>(null);
  const [storageLoading, setStorageLoading] = useState(true);
  const [storageCleaning, setStorageCleaning] = useState(false);
  const [storageError, setStorageError] = useState<string | null>(null);
  const [storageNotice, setStorageNotice] = useState<string | null>(null);
  const [gmail, setGmail] = useState<GmailOutcomeStatus | null>(null);
  const [gmailClientId, setGmailClientId] = useState("");
  const [gmailClientSecret, setGmailClientSecret] = useState("");
  const [gmailBusy, setGmailBusy] = useState(false);
  const [gmailError, setGmailError] = useState<string | null>(null);
  const [gmailNotice, setGmailNotice] = useState<string | null>(null);
  const [manualBrowser, setManualBrowser] = useState<ManualBrowserStatus | null>(null);
  const [manualBrowserBusy, setManualBrowserBusy] = useState(false);
  const [manualBrowserError, setManualBrowserError] = useState<string | null>(null);
  const [resumeUploading, setResumeUploading] = useState(false);
  const [resumeUploadError, setResumeUploadError] = useState<string | null>(null);
  const [pluginImporting, setPluginImporting] = useState(false);
  const resumeInput = useRef<HTMLInputElement>(null);
  const pluginInput = useRef<HTMLInputElement>(null);

  const settingsPayload = useMemo(() => ({
    resume_path: resumePath,
    blocked_domains: blockedDomains,
    sensitive_data: { email, password },
    max_failures: maxFailures,
    browser_headless: browserHeadless,
    auto_reject_after_months: Math.max(1, Math.min(120, Number(autoRejectAfterMonths) || 1)),
    dossier_retention_enabled: retentionEnabled,
    dossier_retention_days: Math.max(1, Math.min(3650, Number(videoRetentionDays) || 14)),
    dossier_delete_after_days: Math.max(
      Math.max(1, Math.min(3650, Number(videoRetentionDays) || 14)),
      Math.min(3650, Number(dossierRetentionDays) || 30),
    ),
    theme,
  }), [
    autoRejectAfterMonths,
    blockedDomains,
    browserHeadless,
    dossierRetentionDays,
    email,
    maxFailures,
    password,
    resumePath,
    retentionEnabled,
    theme,
    videoRetentionDays,
  ]);
  const autosave = useAutosave(settingsPayload, saveSettings, !loading);

  const handleLanguageChange = async (lang: string) => {
    setCurrentLanguage(lang);
    saveLanguagePreference(lang);
    await loadLanguage(lang || "en");
  };

  const refreshGmail = useCallback(async () => {
    try {
      setGmail(await getGmailOutcomeStatus());
      setGmailError(null);
    } catch {
      setGmailError("Could not read Gmail outcome status.");
    }
  }, []);

  const refreshManualBrowser = useCallback(async () => {
    try {
      setManualBrowser(await getManualBrowserStatus());
      setManualBrowserError(null);
    } catch {
      setManualBrowserError("Could not read the remote browser status.");
    }
  }, []);

  useEffect(() => {
    Promise.all([
      getSettings().then((data) => {
        setResumePath(data.resume_path || "");
        setBlockedDomains(data.blocked_domains || []);
        setMaxFailures(data.max_failures || 8);
        setBrowserHeadless(data.browser_headless === true);
        setAutoRejectAfterMonths(String(data.auto_reject_after_months || 1));
        setRetentionEnabled(data.dossier_retention_enabled !== false);
        const videoDays = data.dossier_retention_days || 14;
        const dossierDays = Math.max(videoDays, data.dossier_delete_after_days || 30);
        setVideoRetentionDays(String(videoDays));
        setDossierRetentionDays(String(dossierDays));
        getApplicationAttemptStorage(videoDays, dossierDays)
          .then(setStorage)
          .catch(() => setStorageError("Could not inspect application evidence storage."))
          .finally(() => setStorageLoading(false));
        // Reconcile theme: a value saved on the backend wins over the local
        // default and is applied immediately.
        if (data.theme && data.theme !== getStoredTheme()) {
          setThemeState(data.theme);
          setTheme(data.theme);
        }
        const sens = data.sensitive_data || { email: "", password: "" };
        setEmail(sens.email || "");
        setPassword(sens.password || "");
      }),
      getPlugins().then((res) => setPlugins(res.plugins || [])),
      getGmailOutcomeStatus()
        .then(setGmail)
        .catch(() => setGmailError("Could not read Gmail outcome status.")),
      getManualBrowserStatus()
        .then(setManualBrowser)
        .catch(() => setManualBrowserError("Could not read the remote browser status.")),
    ])
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    const interval = window.setInterval(() => void refreshManualBrowser(), 5000);
    return () => window.clearInterval(interval);
  }, [refreshManualBrowser]);

  useEffect(() => {
    const refreshOnFocus = () => void refreshGmail();
    window.addEventListener("focus", refreshOnFocus);
    return () => window.removeEventListener("focus", refreshOnFocus);
  }, [refreshGmail]);

  useEffect(() => {
    if (loading || requestedSettingsSection() !== "gmail") return;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById("gmail-outcomes")?.scrollIntoView({ block: "start" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [loading]);

  const refreshStorage = async () => {
    setStorageLoading(true);
    setStorageError(null);
    setStorageNotice(null);
    try {
      const videoDays = Math.max(1, Math.min(3650, Number(videoRetentionDays) || 14));
      const dossierDays = Math.max(videoDays, Math.min(3650, Number(dossierRetentionDays) || 30));
      setVideoRetentionDays(String(videoDays));
      setDossierRetentionDays(String(dossierDays));
      setStorage(await getApplicationAttemptStorage(videoDays, dossierDays));
    } catch {
      setStorageError("Could not inspect application evidence storage.");
    } finally {
      setStorageLoading(false);
    }
  };

  const handleCleanupStorage = async () => {
    setStorageCleaning(true);
    setStorageError(null);
    setStorageNotice(null);
    try {
      const videoDays = Math.max(1, Math.min(3650, Number(videoRetentionDays) || 14));
      const dossierDays = Math.max(videoDays, Math.min(3650, Number(dossierRetentionDays) || 30));
      setVideoRetentionDays(String(videoDays));
      setDossierRetentionDays(String(dossierDays));
      const preview = await getApplicationAttemptStorage(videoDays, dossierDays);
      setStorage(preview);
      if (preview.reclaimable_bytes <= 0) {
        setStorageNotice("Nothing is old enough for cleanup with this policy.");
        return;
      }
      const confirmed = window.confirm(
        `Run cleanup and reclaim about ${formatBytes(preview.reclaimable_bytes)}? Video will be removed from ${preview.video_cleanup_dossiers} dossier${preview.video_cleanup_dossiers === 1 ? "" : "s"}. ${preview.dossier_cleanup_dossiers} full dossier${preview.dossier_cleanup_dossiers === 1 ? "" : "s"} will be permanently deleted.`,
      );
      if (!confirmed) return;
      const result = await cleanupApplicationAttempts(videoDays, dossierDays);
      setStorage(result.storage);
      setStorageNotice(
        `Reclaimed ${formatBytes(result.removed_bytes)}: removed ${result.compacted_dossiers} video${result.compacted_dossiers === 1 ? "" : "s"} and deleted ${result.deleted_dossiers} full dossier${result.deleted_dossiers === 1 ? "" : "s"}.`,
      );
    } catch {
      setStorageError("Application evidence cleanup failed.");
    } finally {
      setStorageCleaning(false);
    }
  };

  const handleTogglePlugin = async (name: string, enabled: boolean) => {
    await togglePlugin(name, enabled);
    setPlugins((prev) => prev.map((p) => p.name === name ? { ...p, enabled } : p));
  };

  const handleRemovePlugin = async (name: string) => {
    await removePlugin(name);
    setPlugins((prev) => prev.filter((p) => p.name !== name));
  };

  const handleImportPlugin = async (file: File | undefined) => {
    if (!file) return;
    setPluginImporting(true);
    try {
      await importPlugin(file);
      const res = await getPlugins();
      setPlugins(res.plugins || []);
    } catch (e) {
      alert(e instanceof Error ? e.message : "Failed to import plugin");
    } finally {
      setPluginImporting(false);
      if (pluginInput.current) pluginInput.current.value = "";
    }
  };

  const handleResumeUpload = async (file: File | undefined) => {
    if (!file) return;
    setResumeUploading(true);
    setResumeUploadError(null);
    try {
      const result = await uploadResume(file);
      setResumePath(result.resume_path);
    } catch (error) {
      setResumeUploadError(error instanceof Error ? error.message : "Resume upload failed.");
    } finally {
      setResumeUploading(false);
      if (resumeInput.current) resumeInput.current.value = "";
    }
  };

  const handleThemeChange = (mode: ThemeMode) => {
    setThemeState(mode);
    setTheme(mode); // apply + persist locally immediately
  };

  const handleManualBrowser = async (action: "start" | "stop") => {
    setManualBrowserBusy(true);
    setManualBrowserError(null);
    try {
      const status = action === "start" ? await startManualBrowser() : await stopManualBrowser();
      setManualBrowser(status);
    } catch (error) {
      setManualBrowserError(error instanceof Error ? error.message : `Could not ${action} the remote browser.`);
    } finally {
      setManualBrowserBusy(false);
    }
  };

  const addDomain = () => {
    if (newDomain.trim() && !blockedDomains.includes(newDomain.trim())) {
      setBlockedDomains([...blockedDomains, newDomain.trim()]);
      setNewDomain("");
    }
  };

  const removeDomain = (d: string) => {
    setBlockedDomains(blockedDomains.filter((x) => x !== d));
  };

  const handleConnectGmail = async () => {
    if (!gmail?.client_configured && (!gmailClientId.trim() || !gmailClientSecret.trim())) {
      setGmailError("Enter the client ID and client secret from a Google OAuth client.");
      return;
    }
    setGmailBusy(true);
    setGmailError(null);
    setGmailNotice(null);
    try {
      const result = await connectGmailOutcomes(gmailClientId.trim(), gmailClientSecret.trim());
      setGmailClientSecret("");
      await openTrustedExternalUrl(result.authorization_url, "_self");
      setGmailNotice("Complete the read-only Gmail authorization in the browser, then return to Hunter.");
      window.setTimeout(() => void refreshGmail(), 4000);
    } catch (error) {
      setGmailError(error instanceof Error ? error.message : "Could not connect Gmail.");
    } finally {
      setGmailBusy(false);
    }
  };

  const handleSyncGmail = async () => {
    setGmailBusy(true);
    setGmailError(null);
    setGmailNotice(null);
    try {
      const result = await syncGmailOutcomes();
      setGmail(result.status);
      setGmailNotice(
        `Scanned ${result.scanned} new email${result.scanned === 1 ? "" : "s"}, updated ${result.updated}, and flagged ${result.review_required} for review.`,
      );
    } catch (error) {
      setGmailError(error instanceof Error ? error.message : "Gmail synchronization failed.");
    } finally {
      setGmailBusy(false);
    }
  };

  const handleDisconnectGmail = async () => {
    if (!window.confirm("Disconnect Gmail outcome synchronization?")) return;
    setGmailBusy(true);
    setGmailError(null);
    try {
      await disconnectGmailOutcomes();
      await refreshGmail();
      setGmailNotice("Gmail outcome synchronization is disconnected.");
    } catch (error) {
      setGmailError(error instanceof Error ? error.message : "Could not disconnect Gmail.");
    } finally {
      setGmailBusy(false);
    }
  };

  if (loading) return <LoadingSpinner />;

  return (
    <div className="max-w-4xl">
      <PageHeader
        title="Settings"
        subtitle="General application settings"
        actions={
          <>
          <button type="button" onClick={() => { window.location.hash = "/llm"; }} className="btn-secondary">
            <Cpu className="w-4 h-4" /> AI settings
          </button>
          <span className="flex items-center gap-1.5 text-sm text-muted-foreground" role="status" aria-live="polite">
            {autosave.status === "pending" || autosave.status === "saving" ? (
              <><Loader2 className="h-4 w-4 animate-spin" /> Saving…</>
            ) : autosave.status === "saved" ? (
              <><CheckCircle className="h-4 w-4 text-success" /> Saved</>
            ) : (
              "Changes save automatically"
            )}
          </span>
          </>
        }
      />
      {autosave.status === "error" && (
        <div className="error-banner mb-5 flex items-center justify-between">
          Failed to save settings. Please try again.
          <div className="flex items-center gap-2">
            <button type="button" onClick={autosave.retry} className="btn-secondary">Retry</button>
            <button type="button" onClick={autosave.clearError} aria-label="Dismiss save error" className="text-destructive hover:text-destructive/80"><X className="w-4 h-4" /></button>
          </div>
        </div>
      )}

      {/* Language */}
      <Section title={t("language.title")}>
        <div>
          <label className="block text-sm font-semibold text-foreground mb-1.5">{t("language.label")}</label>
          <select
            value={currentLanguage}
            onChange={(e) => handleLanguageChange(e.target.value)}
            className="w-full px-3 py-2 border border-border rounded-lg text-sm bg-card max-w-xs"
          >
            <option value="">{t("language.auto")}</option>
            {Object.entries(LANGUAGE_NAMES).map(([code, name]) => (
              <option key={code} value={code}>{name}</option>
            ))}
          </select>
          <p className="text-[13px] text-muted-foreground mt-1.5">{t("language.description")}</p>
        </div>
      </Section>

      {/* Appearance / Theme */}
      <Section title={t("appearance.title", "Appearance")}>
        <label className="block text-sm font-semibold text-foreground mb-2">{t("appearance.theme", "Theme")}</label>
        <div className="flex gap-2" role="radiogroup" aria-label={t("appearance.theme", "Theme")}>
          {([
            { mode: "light" as const, label: t("appearance.light", "Light"), Icon: Sun },
            { mode: "dark" as const, label: t("appearance.dark", "Dark"), Icon: Moon },
            { mode: "system" as const, label: t("appearance.system", "System"), Icon: Monitor },
          ]).map(({ mode, label, Icon }) => (
            <button
              key={mode}
              role="radio"
              aria-checked={theme === mode}
              onClick={() => handleThemeChange(mode)}
              className={`filter-tab flex items-center gap-1.5 ${theme === mode ? "filter-tab-active" : "filter-tab-inactive"}`}
            >
              <Icon className="w-3.5 h-3.5" /> {label}
            </button>
          ))}
        </div>
        <p className="text-[13px] text-muted-foreground mt-2">{t("appearance.description", "Choose light, dark, or follow your system setting.")}</p>
      </Section>

      {/* Resume */}
      <Section title="Resume">
        <div className="flex gap-2">
          <input value={resumePath} onChange={(e) => setResumePath(e.target.value)}
            placeholder="/path/to/your/resume.pdf"
            className="input-base flex-1" />
          <input
            ref={resumeInput}
            type="file"
            accept="application/pdf,.pdf"
            className="hidden"
            onChange={(event) => void handleResumeUpload(event.target.files?.[0])}
          />
          <button
            type="button"
            disabled={resumeUploading}
            onClick={() => resumeInput.current?.click()}
            className="btn-secondary disabled:opacity-50"
          >
            {resumeUploading
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <FolderOpen className="w-4 h-4" />}
            Upload PDF
          </button>
        </div>
        <p className="text-[13px] text-muted-foreground mt-2">
          Upload a PDF to the Hunter host, or enter an existing path on that computer.
        </p>
        {resumeUploadError && (
          <p className="text-[13px] text-destructive mt-2" role="alert">{resumeUploadError}</p>
        )}
      </Section>

      {/* Sensitive Data */}
      <Section title="Account Credentials">
        <p className="text-[13px] text-muted-foreground mb-4">
          Used for creating accounts on external ATS platforms during applications.
          Stored locally in plaintext. Prefer using SSO (Sign in with LinkedIn/Google) when possible.
        </p>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-semibold text-foreground mb-1.5">Email</label>
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
              className="input-base" />
          </div>
          <div>
            <label className="block text-sm font-semibold text-foreground mb-1.5">Password</label>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
              className="input-base" />
          </div>
        </div>
      </Section>

      <div id="gmail-outcomes" className="scroll-mt-4">
        <Section title="Gmail hiring outcomes">
          <div className="space-y-4">
            <div className="flex items-start gap-3">
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Mail className="h-5 w-5" />
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-semibold text-foreground">
                  {gmail?.connected
                    ? `Connected as ${gmail.account_email || "your Gmail account"}`
                    : "Connect Gmail with read-only access"}
                </p>
                <p className="mt-1 text-[13px] leading-5 text-muted-foreground">
                  Hunter checks new mail every five minutes. It updates explicit assessments,
                  rejections, interview invitations, and offers that match a saved application exactly.
                </p>
              </div>
            </div>

            {!gmail?.connected && (
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="text-sm font-semibold text-foreground">
                  Google OAuth client ID
                  <input
                    value={gmailClientId}
                    onChange={(event) => setGmailClientId(event.target.value)}
                    placeholder={gmail?.client_configured ? "Leave blank to reuse saved credentials" : "…apps.googleusercontent.com"}
                    className="input-base mt-1.5"
                  />
                </label>
                <label className="text-sm font-semibold text-foreground">
                  Google OAuth client secret
                  <input
                    type="password"
                    value={gmailClientSecret}
                    onChange={(event) => setGmailClientSecret(event.target.value)}
                    placeholder={gmail?.client_configured ? "Leave blank to reuse saved credentials" : "Client secret"}
                    className="input-base mt-1.5"
                  />
                </label>
                <p className="text-xs leading-5 text-muted-foreground sm:col-span-2">
                  Use a Google OAuth Desktop client for localhost or a Web client configured with Hunter's private HTTPS callback. Credentials and refresh tokens stay in Hunter's data directory with owner-only permissions.
                </p>
              </div>
            )}

            <div className="grid gap-3 sm:grid-cols-3">
              <div className="panel-inset">
                <p className="eyebrow">Last successful sync</p>
                <p className="mt-1 text-sm font-semibold text-foreground">{formatDateTime(gmail?.last_success_at)}</p>
              </div>
              <div className="panel-inset">
                <p className="eyebrow">Pending reviews</p>
                <p className="mt-1 text-sm font-semibold text-foreground">{gmail?.review_required_count || 0}</p>
              </div>
              <div className="panel-inset">
                <p className="eyebrow">Telegram alerts</p>
                <p className={`mt-1 text-sm font-semibold ${gmail?.telegram_configured ? "text-success" : "text-warning"}`}>
                  {gmail?.telegram_configured ? "Configured" : "Not configured"}
                </p>
              </div>
            </div>

            {gmail?.last_sync_summary && (
              <p className="text-xs text-muted-foreground">
                Latest sync only: scanned {gmail.last_sync_summary.scanned}, updated {gmail.last_sync_summary.updated}, and added {gmail.last_sync_summary.review_required} pending review{gmail.last_sync_summary.review_required === 1 ? "" : "s"}.
              </p>
            )}
            {(gmailError || gmail?.last_error) && (
              <div className="error-banner">{gmailError || gmail?.last_error}</div>
            )}
            {gmailNotice && (
              <div className="rounded-lg border border-success/30 bg-success/10 p-3 text-sm text-foreground">
                {gmailNotice}
              </div>
            )}

            <div className="flex flex-wrap gap-2">
              {!gmail?.connected ? (
                <button type="button" onClick={() => void handleConnectGmail()} disabled={gmailBusy} className="btn-primary">
                  {gmailBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mail className="h-4 w-4" />}
                  Connect Gmail
                </button>
              ) : (
                <>
                  <button type="button" onClick={() => void handleSyncGmail()} disabled={gmailBusy} className="btn-primary">
                    {gmailBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                    Sync now
                  </button>
                  <button type="button" onClick={() => void handleConnectGmail()} disabled={gmailBusy} className="btn-secondary">
                    Reauthorize
                  </button>
                  <button type="button" onClick={() => void handleDisconnectGmail()} disabled={gmailBusy} className="btn-secondary">
                    Disconnect
                  </button>
                </>
              )}
              <button type="button" onClick={() => void refreshGmail()} disabled={gmailBusy} className="btn-secondary">
                Refresh status
              </button>
            </div>

            {!!gmail?.review_required_count && (
              <div className="flex flex-col gap-3 border-t border-border pt-4 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-sm font-semibold text-foreground">
                    {gmail.review_required_count} email outcome{gmail.review_required_count === 1 ? "" : "s"} awaiting review
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Review ambiguous hiring emails independently from stored job outcomes.
                  </p>
                </div>
                <a href="#/jobs?tab=pending&category=rejected&review=email" className="btn-secondary shrink-0">
                  Review emails <ArrowRight className="h-4 w-4" />
                </a>
              </div>
            )}
          </div>
        </Section>
      </div>

      {/* Agent Settings */}
      <Section title="Agent Settings">
        <div className="space-y-5">
          <div className="rounded-lg border border-border bg-secondary/45 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="flex items-center gap-2">
                  <Monitor className="h-4 w-4 text-primary" />
                  <span className="text-sm font-semibold text-foreground">Remote manual browser</span>
                  {manualBrowser && (
                    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                      manualBrowser.state === "running"
                        ? "bg-success/10 text-success"
                        : manualBrowser.state === "error"
                          ? "bg-destructive/10 text-destructive"
                          : "bg-muted text-muted-foreground"
                    }`}>
                      {manualBrowser.state}
                    </span>
                  )}
                </div>
                <p className="mt-1.5 max-w-2xl text-[13px] leading-5 text-muted-foreground">
                  Opens an idle Chrome with Hunter&apos;s saved profile on a private virtual display. Connect from your Mac with <code>scripts/connect-remote-browser.sh</code>; it copies the one-session VNC password to your clipboard.
                </p>
              </div>
              <div className="flex gap-2">
                {manualBrowser?.state === "running" || manualBrowser?.state === "starting" || manualBrowser?.state === "stopping" ? (
                  <button
                    type="button"
                    onClick={() => void handleManualBrowser("stop")}
                    disabled={manualBrowserBusy || manualBrowser.state === "stopping"}
                    className="btn-secondary disabled:opacity-50"
                  >
                    {manualBrowserBusy || manualBrowser.state === "stopping" ? <Loader2 className="h-4 w-4 animate-spin" /> : <X className="h-4 w-4" />}
                    Stop browser
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => void handleManualBrowser("start")}
                    disabled={manualBrowserBusy || !manualBrowser?.supported || !manualBrowser?.dependencies_ready}
                    className="btn-primary disabled:opacity-50"
                  >
                    {manualBrowserBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Monitor className="h-4 w-4" />}
                    Start browser
                  </button>
                )}
              </div>
            </div>
            {manualBrowser?.message && (
              <p className={`mt-3 text-sm ${manualBrowser.state === "error" ? "text-destructive" : "text-foreground"}`}>
                {manualBrowser.message}
              </p>
            )}
            {manualBrowser && !manualBrowser.supported && (
              <p className="mt-3 text-sm text-muted-foreground">This control is available on the Linux remote host.</p>
            )}
            {manualBrowser?.supported && !manualBrowser.dependencies_ready && (
              <p className="mt-3 text-sm text-warning">
                Missing {manualBrowser.missing_dependencies.join(" and ")}. Run <code>scripts/setup-remote-browser.sh</code> once from your Mac.
              </p>
            )}
            {manualBrowserError && <p className="mt-3 text-sm text-destructive">{manualBrowserError}</p>}
          </div>
          <div>
            <label className="block text-sm font-semibold text-foreground mb-2">Browser Mode</label>
            <div className="flex gap-2" role="radiogroup" aria-label="Browser Mode">
              <button
                type="button"
                role="radio"
                aria-checked={!browserHeadless}
                onClick={() => setBrowserHeadless(false)}
                className={`filter-tab ${!browserHeadless ? "filter-tab-active" : "filter-tab-inactive"}`}
              >
                Headed
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={browserHeadless}
                onClick={() => setBrowserHeadless(true)}
                className={`filter-tab ${browserHeadless ? "filter-tab-active" : "filter-tab-inactive"}`}
              >
                Headless
              </button>
            </div>
            <p className="text-[13px] text-muted-foreground mt-2">
              Use headed mode to sign in or debug. Headless mode hides agent browser windows and requires existing valid sessions.
            </p>
          </div>
          <div>
            <label className="block text-sm font-semibold text-foreground mb-1.5">Max Failures Per Job</label>
            <input type="number" value={maxFailures} onChange={(e) => {
              const val = Number(e.target.value);
              setMaxFailures(Math.max(1, Math.min(50, isNaN(val) ? 8 : val)));
            }}
              min={1} max={50} className="input-base !w-32" />
            <p className="text-[13px] text-muted-foreground mt-1.5">
              Agent stops trying after this many consecutive failures on a single job.
            </p>
          </div>
          <div>
            <label htmlFor="auto-reject-after-months" className="block text-sm font-semibold text-foreground mb-1.5">
              Move unanswered applications to Rejected after
            </label>
            <div className="flex items-center gap-2">
              <input
                id="auto-reject-after-months"
                type="number"
                min={1}
                max={120}
                value={autoRejectAfterMonths}
                onChange={(event) => setAutoRejectAfterMonths(event.target.value)}
                className="input-base !w-32"
              />
              <span className="text-sm text-muted-foreground">months</span>
            </div>
            <p className="text-[13px] text-muted-foreground mt-1.5">
              Only applications still in Applied are moved. Interviews, offers, and manually recorded outcomes stay unchanged.
            </p>
          </div>
        </div>
      </Section>

      {/* Blocked Domains */}
      <Section title={t("blockedDomains.title")} className="">
        <p className="text-[13px] text-muted-foreground mb-4">{t("blockedDomains.description")}</p>
        <TagInput
          tags={blockedDomains}
          value={newDomain}
          onChange={setNewDomain}
          onAdd={addDomain}
          onRemove={removeDomain}
          placeholder="example.com"
          variant="destructive"
        />
        {blockedDomains.length === 0 && (
          <p className="text-[13px] text-muted-foreground mt-2">No blocked domains</p>
        )}
      </Section>

      <Section title="Application evidence storage">
        <div className="space-y-4">
          <div className="flex items-start gap-3">
            <span className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <HardDrive className="h-5 w-5" />
            </span>
            <div>
              <p className="text-sm font-semibold text-foreground">Automatic two-stage cleanup</p>
              <p className="mt-1 text-[13px] leading-5 text-muted-foreground">
                Hunter first removes recording video while keeping screenshots and written evidence. Later it permanently deletes the entire completed dossier. Active or incomplete attempts are never touched.
              </p>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <div className="panel-inset">
              <p className="eyebrow">Stored</p>
              <p className="mt-1 text-xl font-semibold tabular-nums text-foreground">
                {storageLoading && !storage ? "…" : formatBytes(storage?.total_bytes || 0)}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">{storage?.total_dossiers || 0} dossiers</p>
            </div>
            <div className="panel-inset">
              <p className="eyebrow">Video</p>
              <p className="mt-1 text-xl font-semibold tabular-nums text-foreground">
                {storageLoading && !storage ? "…" : formatBytes(storage?.video_bytes || 0)}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">Recordings and temporary frames</p>
            </div>
            <div className="panel-inset">
              <p className="eyebrow">Can reclaim</p>
              <p className="mt-1 text-xl font-semibold tabular-nums text-success">
                {storageLoading && !storage ? "…" : formatBytes(storage?.reclaimable_bytes || 0)}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">{storage?.reclaimable_dossiers || 0} eligible dossiers</p>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-sm font-semibold text-foreground">
              Remove video after
              <div className="mt-1.5 flex items-center gap-2">
                <input
                  type="number"
                  min={1}
                  max={3650}
                  value={videoRetentionDays}
                  onChange={(event) => setVideoRetentionDays(event.target.value)}
                  className="input-base !w-28"
                />
                <span className="text-sm font-normal text-muted-foreground">days</span>
              </div>
            </label>
            <label className="text-sm font-semibold text-foreground">
              Delete full dossier after
              <div className="mt-1.5 flex items-center gap-2">
                <input
                  type="number"
                  min={1}
                  max={3650}
                  value={dossierRetentionDays}
                  onChange={(event) => setDossierRetentionDays(event.target.value)}
                  className="input-base !w-28"
                />
                <span className="text-sm font-normal text-muted-foreground">days</span>
              </div>
            </label>
          </div>

          <label className="flex items-start gap-3 rounded-lg border border-border bg-secondary/45 p-3">
            <input
              type="checkbox"
              checked={retentionEnabled}
              onChange={(event) => setRetentionEnabled(event.target.checked)}
              className="mt-0.5 h-4 w-4 accent-primary"
            />
            <span>
              <span className="block text-sm font-semibold text-foreground">Clean up automatically</span>
              <span className="mt-0.5 block text-xs leading-5 text-muted-foreground">
                Apply this policy after completed attempts and when Hunter starts. Disable it to keep cleanup manual.
              </span>
            </span>
          </label>

          {storage?.protected_incomplete_dossiers ? (
            <p className="rounded-lg bg-warning/10 px-3 py-2 text-xs text-warning">
              {storage.protected_incomplete_dossiers} active or unreadable dossier{storage.protected_incomplete_dossiers === 1 ? " is" : "s are"} protected from cleanup.
            </p>
          ) : null}
          {storage?.dossier_cleanup_dossiers ? (
            <p className="rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">
              Current preview will permanently delete {storage.dossier_cleanup_dossiers} full dossier{storage.dossier_cleanup_dossiers === 1 ? "" : "s"} ({formatBytes(storage.dossier_cleanup_bytes)}).
            </p>
          ) : null}
          {storageError && <p className="text-sm text-destructive">{storageError}</p>}
          {storageNotice && <p className="text-sm text-success">{storageNotice}</p>}

          <div className="flex flex-wrap gap-2">
            <button type="button" onClick={() => void refreshStorage()} disabled={storageLoading || storageCleaning} className="btn-secondary disabled:opacity-50">
              {storageLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              Preview cleanup
            </button>
            <button type="button" onClick={() => void handleCleanupStorage()} disabled={storageLoading || storageCleaning} className="btn-primary disabled:opacity-50">
              {storageCleaning ? <Loader2 className="h-4 w-4 animate-spin" /> : <HardDrive className="h-4 w-4" />}
              Run cleanup now
            </button>
          </div>
        </div>
      </Section>

      {/* Plugins */}
      <Section title="Job Source Plugins">
        <p className="text-[13px] text-muted-foreground mb-4">
          Manage job source plugins. Built-in plugins provide LinkedIn, Indeed, SEEK, Naukri, Reed, and StepStone.
          Import community plugins (.yaml files) for additional job sites.
        </p>
        <div className="space-y-3 mb-4">
          {plugins.map((plugin) => (
            <div key={plugin.name} className="flex items-center justify-between p-3 border border-border rounded-lg">
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold text-foreground">{plugin.display_name}</span>
                  <span className="text-[11px] px-1.5 py-0.5 rounded bg-secondary text-muted-foreground">
                    v{plugin.version}
                  </span>
                  {plugin.is_builtin && (
                    <span className="text-[11px] px-1.5 py-0.5 rounded bg-primary/10 text-primary">Built-in</span>
                  )}
                </div>
                <p className="text-[12px] text-muted-foreground mt-0.5">
                  {plugin.description} &middot; {plugin.countries.join(", ")}
                </p>
              </div>
              <div className="flex items-center gap-2 ml-4">
                <label className="relative inline-flex items-center cursor-pointer">
                  <input
                    type="checkbox"
                    checked={plugin.enabled}
                    onChange={(e) => handleTogglePlugin(plugin.name, e.target.checked)}
                    className="sr-only peer"
                  />
                  <div className="w-9 h-5 bg-secondary peer-focus:ring-2 peer-focus:ring-primary/20 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary"></div>
                </label>
                {!plugin.is_builtin && (
                  <button onClick={() => handleRemovePlugin(plugin.name)} className="text-destructive hover:text-destructive/80">
                    <Trash2 className="w-4 h-4" />
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
        <input
          ref={pluginInput}
          type="file"
          accept=".yaml,.yml,application/yaml,text/yaml"
          className="hidden"
          onChange={(event) => void handleImportPlugin(event.target.files?.[0])}
        />
        <button
          type="button"
          disabled={pluginImporting}
          onClick={() => pluginInput.current?.click()}
          className="btn-secondary disabled:opacity-50"
        >
          {pluginImporting
            ? <Loader2 className="w-4 h-4 animate-spin" />
            : <Upload className="w-4 h-4" />}
          Import Plugin
        </button>
      </Section>
    </div>
  );
}
