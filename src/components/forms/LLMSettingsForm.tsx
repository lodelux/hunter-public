import { useState, useEffect, useRef, useCallback } from "react";
import { TestTube, CheckCircle, XCircle, Loader2, ChevronDown, ChevronUp, ExternalLink } from "lucide-react";
import { getLLMSettings, saveLLMSettings, testLLMConnection } from "../../lib/api";
import type { LLMProvider, LLMSettings } from "../../lib/types";
import { useTranslation } from "react-i18next";

const PROVIDERS: { id: LLMProvider; nameKey: string; descKey: string }[] = [
  { id: "openrouter", nameKey: "providers.openrouter.name", descKey: "providers.openrouter.description" },
  { id: "openai", nameKey: "providers.openai.name", descKey: "providers.openai.description" },
  { id: "anthropic", nameKey: "providers.anthropic.name", descKey: "providers.anthropic.description" },
  { id: "gemini", nameKey: "providers.gemini.name", descKey: "providers.gemini.description" },
  { id: "bedrock", nameKey: "providers.bedrock.name", descKey: "providers.bedrock.description" },
  { id: "ollama", nameKey: "providers.ollama.name", descKey: "providers.ollama.description" },
  { id: "openai_compatible", nameKey: "providers.openai_compatible.name", descKey: "providers.openai_compatible.description" },
];

const defaultSettings: LLMSettings = {
  provider: "openrouter",
  openai: { api_key: "", model: "gpt-5.6-sol" },
  anthropic: { api_key: "", model: "claude-sonnet-4-5" },
  gemini: { api_key: "", model: "gemini-2.5-pro" },
  bedrock: { access_key: "", secret_key: "", region: "us-west-2", model: "us.anthropic.claude-sonnet-4-6", auth_mode: "profile", profile_name: "default" },
  ollama: { base_url: "http://localhost:11434", model: "" },
  openrouter: { api_key: "", model: "qwen/qwen3.6-plus" },
  openai_compatible: { base_url: "", api_key: "", model: "" },
};

const PROVIDER_DISPLAY_NAMES: Record<string, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  gemini: "Google Gemini",
  bedrock: "AWS Bedrock",
  ollama: "Ollama",
  openrouter: "OpenRouter",
  openai_compatible: "OpenAI-Compatible",
};

interface LLMSettingsFormProps {
  onSaved?: () => void;
  compact?: boolean;
}

export default function LLMSettingsForm({ onSaved, compact }: LLMSettingsFormProps) {
  const { t } = useTranslation("llm");
  const [settings, setSettings] = useState<LLMSettings>(defaultSettings);
  const [testStatus, setTestStatus] = useState<"idle" | "testing" | "success" | "error">("idle");
  const [testMessage, setTestMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(true);
  const [showGuide, setShowGuide] = useState(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSettings = useRef<LLMSettings | null>(null);
  const saveQueue = useRef<Promise<void>>(Promise.resolve());
  const mounted = useRef(true);

  useEffect(() => {
    let active = true;
    getLLMSettings()
      .then((data) => {
        if (active) setSettings({ ...defaultSettings, ...data });
      })
      .catch(() => {})
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, []);

  const persist = useCallback((newSettings: LLMSettings) => {
    saveQueue.current = saveQueue.current
      .catch(() => {})
      .then(async () => {
        if (mounted.current) {
          setSaving(true);
          setSaved(false);
        }
        try {
          await saveLLMSettings(newSettings);
          if (mounted.current) {
            setSaved(true);
            setTimeout(() => {
              if (mounted.current) setSaved(false);
            }, 2000);
          }
          onSaved?.();
        } catch {
          // Keep the latest local value; the next edit will retry persistence.
        } finally {
          if (mounted.current) setSaving(false);
        }
      });
    return saveQueue.current;
  }, [onSaved]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (saveTimer.current) clearTimeout(saveTimer.current);
      const pending = pendingSettings.current;
      pendingSettings.current = null;
      if (pending) void persist(pending);
    };
  }, [persist]);

  // Autosave with debounce
  const autoSave = useCallback((newSettings: LLMSettings) => {
    setSaved(false);
    pendingSettings.current = newSettings;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      pendingSettings.current = null;
      void persist(newSettings);
    }, 800);
  }, [persist]);

  // Immediate save (for dropdown/radio changes)
  const immediateSave = useCallback((newSettings: LLMSettings) => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    pendingSettings.current = null;
    void persist(newSettings);
  }, [persist]);

  const updateProvider = (provider: LLMProvider) => {
    const ns = { ...settings, provider };
    setSettings(ns);
    setTestStatus("idle");
    immediateSave(ns);
  };

  const updateOpenAI = (field: string, value: string) => {
    const ns = { ...settings, openai: { ...settings.openai!, [field]: value } };
    setSettings(ns);
    autoSave(ns);
  };

  const updateAnthropic = (field: string, value: string) => {
    const ns = { ...settings, anthropic: { ...settings.anthropic!, [field]: value } };
    setSettings(ns);
    autoSave(ns);
  };

  const updateGemini = (field: string, value: string) => {
    const ns = { ...settings, gemini: { ...settings.gemini!, [field]: value } };
    setSettings(ns);
    autoSave(ns);
  };

  const updateBedrock = (field: string, value: string) => {
    const ns = { ...settings, bedrock: { ...settings.bedrock!, [field]: value } };
    setSettings(ns);
    if (field === "auth_mode") immediateSave(ns); else autoSave(ns);
  };

  const updateOllama = (field: string, value: string) => {
    const ns = { ...settings, ollama: { ...settings.ollama!, [field]: value } };
    setSettings(ns);
    autoSave(ns);
  };

  const updateOpenRouter = (field: string, value: string) => {
    const ns = { ...settings, openrouter: { ...settings.openrouter!, [field]: value } };
    setSettings(ns);
    autoSave(ns);
  };

  const updateOpenAICompatible = (field: string, value: string) => {
    const ns = { ...settings, openai_compatible: { ...settings.openai_compatible!, [field]: value } };
    setSettings(ns);
    autoSave(ns);
  };

  const handleTest = async () => {
    setTestStatus("testing");
    setTestMessage("");
    try {
      const result = await testLLMConnection(settings);
      setTestStatus(result.success ? "success" : "error");
      setTestMessage(result.message || (result.success ? t("status.connectionSuccessful") : t("status.connectionFailed")));
    } catch (e) {
      setTestStatus("error");
      setTestMessage(e instanceof Error ? e.message : t("status.connectionFailed"));
    }
  };

  if (loading) return <div className="flex justify-center py-4"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>;

  return (
    <div className="space-y-4">
      {/* API Key Guide — OpenAI & Anthropic only */}
      {(settings.provider === "openai" || settings.provider === "anthropic" || settings.provider === "openrouter") && (
        <div className="rounded-2xl border border-border overflow-hidden">
          <button
            onClick={() => setShowGuide(!showGuide)}
            className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-secondary/50 transition-colors"
          >
            <span className="text-[13px] font-semibold text-primary">
              {t("guide.howToGetKey", { provider: PROVIDER_DISPLAY_NAMES[settings.provider] })}
            </span>
            {showGuide ? <ChevronUp className="w-4 h-4 text-muted-foreground" /> : <ChevronDown className="w-4 h-4 text-muted-foreground" />}
          </button>

          {showGuide && (
            <div className="px-4 pb-4 border-t border-border pt-3">
              {settings.provider === "openai" ? (
                <div className="space-y-4">
                  <div className="space-y-2.5">
                    {[
                      { step: 1, text: t("guide.openai.step1"), link: "https://platform.openai.com/signup", linkText: t("guide.openai.step1Link") },
                      { step: 2, text: t("guide.openai.step2"), link: "https://platform.openai.com/settings/organization/billing/overview", linkText: t("guide.openai.step2Link") },
                      { step: 3, text: t("guide.openai.step3"), link: "https://platform.openai.com/api-keys", linkText: t("guide.openai.step3Link") },
                      { step: 4, text: t("guide.openai.step4") },
                      { step: 5, text: t("guide.openai.step5") },
                    ].map(({ step, text, link, linkText }) => (
                      <div key={step} className="flex gap-3">
                        <div className="w-6 h-6 rounded-full bg-secondary flex items-center justify-center flex-shrink-0 mt-0.5">
                          <span className="text-xs font-bold text-foreground">{step}</span>
                        </div>
                        <div className="text-[13px] text-foreground leading-relaxed">
                          {text}
                          {link && (
                            <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary font-semibold hover:underline ml-1">
                              {linkText} <ExternalLink className="w-3 h-3" />
                            </a>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="rounded-lg bg-secondary p-3 text-[12px] text-muted-foreground">
                    <strong className="text-foreground">{t("guide.note")}</strong> {t("guide.openai.note")}
                  </div>
                </div>
              ) : settings.provider === "anthropic" ? (
                <div className="space-y-4">
                  <div className="space-y-2.5">
                    {[
                      { step: 1, text: t("guide.anthropic.step1"), link: "https://console.anthropic.com/signup", linkText: t("guide.anthropic.step1Link") },
                      { step: 2, text: t("guide.anthropic.step2"), link: "https://console.anthropic.com/settings/billing", linkText: t("guide.anthropic.step2Link") },
                      { step: 3, text: t("guide.anthropic.step3"), link: "https://console.anthropic.com/settings/keys", linkText: t("guide.anthropic.step3Link") },
                      { step: 4, text: t("guide.anthropic.step4") },
                      { step: 5, text: t("guide.anthropic.step5") },
                    ].map(({ step, text, link, linkText }) => (
                      <div key={step} className="flex gap-3">
                        <div className="w-6 h-6 rounded-full bg-secondary flex items-center justify-center flex-shrink-0 mt-0.5">
                          <span className="text-xs font-bold text-foreground">{step}</span>
                        </div>
                        <div className="text-[13px] text-foreground leading-relaxed">
                          {text}
                          {link && (
                            <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary font-semibold hover:underline ml-1">
                              {linkText} <ExternalLink className="w-3 h-3" />
                            </a>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="rounded-lg bg-secondary p-3 text-[12px] text-muted-foreground">
                    <strong className="text-foreground">{t("guide.note")}</strong> {t("guide.anthropic.note")}
                  </div>
                </div>
              ) : (
                <div className="space-y-4">
                  <div className="space-y-2.5">
                    {[
                      { step: 1, text: t("guide.openrouter.step1"), link: "https://openrouter.ai", linkText: t("guide.openrouter.step1Link") },
                      { step: 2, text: t("guide.openrouter.step2"), link: "https://openrouter.ai/keys", linkText: t("guide.openrouter.step2Link") },
                      { step: 3, text: t("guide.openrouter.step3") },
                      { step: 4, text: t("guide.openrouter.step4") },
                    ].map(({ step, text, link, linkText }) => (
                      <div key={step} className="flex gap-3">
                        <div className="w-6 h-6 rounded-full bg-secondary flex items-center justify-center flex-shrink-0 mt-0.5">
                          <span className="text-xs font-bold text-foreground">{step}</span>
                        </div>
                        <div className="text-[13px] text-foreground leading-relaxed">
                          {text}
                          {link && (
                            <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary font-semibold hover:underline ml-1">
                              {linkText} <ExternalLink className="w-3 h-3" />
                            </a>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="rounded-lg bg-secondary p-3 text-[12px] text-muted-foreground">
                    <strong className="text-foreground">{t("guide.note")}</strong> {t("guide.openrouter.note")}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Provider Selection */}
      <div className={compact ? "" : "card"}>
        {!compact && <h3 className="text-sm font-bold text-foreground mb-4">{t("selectProvider")}</h3>}
        <div className="space-y-2">
          {PROVIDERS.map(({ id, nameKey, descKey }) => (
            <label key={id} className={`flex items-start gap-3 p-4 rounded-2xl border cursor-pointer transition-all ${settings.provider === id ? "border-foreground bg-secondary" : "border-border hover:border-gray-300"}`}>
              <input type="radio" name="provider" value={id} checked={settings.provider === id} onChange={() => updateProvider(id)} className="mt-1" />
              <div>
                <p className="text-sm font-medium text-foreground">{t(nameKey)}</p>
                <p className="text-xs text-muted-foreground">{t(descKey)}</p>
              </div>
            </label>
          ))}
        </div>
      </div>

      {/* Provider Config */}
      <div className={compact ? "" : "card"}>
        {!compact && <h3 className="text-sm font-bold text-foreground mb-4">{t("configuration", { provider: PROVIDER_DISPLAY_NAMES[settings.provider] })}</h3>}

        {settings.provider === "openai" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.apiKey")}</label>
              <input type="password" value={settings.openai?.api_key || ""} onChange={(e) => updateOpenAI("api_key", e.target.value)} placeholder="sk-..." className="input-base font-mono" />
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.model")}</label>
              <input type="text" value={settings.openai?.model || ""} onChange={(e) => updateOpenAI("model", e.target.value)} placeholder="gpt-5.6-sol" className="input-base font-mono" />
            </div>
          </div>
        )}

        {settings.provider === "anthropic" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.apiKey")}</label>
              <input type="password" value={settings.anthropic?.api_key || ""} onChange={(e) => updateAnthropic("api_key", e.target.value)} placeholder="sk-ant-..." className="input-base font-mono" />
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.model")}</label>
              <input type="text" value={settings.anthropic?.model || ""} onChange={(e) => updateAnthropic("model", e.target.value)} placeholder="claude-sonnet-4-5" className="input-base font-mono" />
            </div>
          </div>
        )}

        {settings.provider === "gemini" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.apiKey")}</label>
              <input type="password" value={settings.gemini?.api_key || ""} onChange={(e) => updateGemini("api_key", e.target.value)} placeholder="AIza..." className="input-base font-mono" />
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.model")}</label>
              <input type="text" value={settings.gemini?.model || ""} onChange={(e) => updateGemini("model", e.target.value)} placeholder="gemini-2.5-pro" className="input-base font-mono" />
            </div>
          </div>
        )}

        {settings.provider === "bedrock" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-medium text-foreground mb-2">{t("labels.authentication")}</label>
              <div className="flex gap-2">
                <button onClick={() => updateBedrock("auth_mode", "profile")} className={`filter-tab ${(settings.bedrock?.auth_mode || "profile") === "profile" ? "filter-tab-active" : "filter-tab-inactive"}`}>{t("labels.awsCliProfile")}</button>
                <button onClick={() => updateBedrock("auth_mode", "keys")} className={`filter-tab ${settings.bedrock?.auth_mode === "keys" ? "filter-tab-active" : "filter-tab-inactive"}`}>{t("labels.accessKeySecretKey")}</button>
              </div>
            </div>
            {(settings.bedrock?.auth_mode || "profile") === "profile" ? (
              <div>
                <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.profileName")}</label>
                <input value={settings.bedrock?.profile_name || "default"} onChange={(e) => updateBedrock("profile_name", e.target.value)} className="input-base" />
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.accessKey")}</label>
                  <input type="password" value={settings.bedrock?.access_key || ""} onChange={(e) => updateBedrock("access_key", e.target.value)} placeholder="AKIA..." className="input-base font-mono" />
                </div>
                <div>
                  <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.secretKey")}</label>
                  <input type="password" value={settings.bedrock?.secret_key || ""} onChange={(e) => updateBedrock("secret_key", e.target.value)} className="input-base font-mono" />
                </div>
              </div>
            )}
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.region")}</label>
              <input value={settings.bedrock?.region || "us-west-2"} onChange={(e) => updateBedrock("region", e.target.value)} className="input-base" />
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.model")}</label>
              <input type="text" value={settings.bedrock?.model || ""} onChange={(e) => updateBedrock("model", e.target.value)} placeholder="us.anthropic.claude-sonnet-4-6" className="input-base font-mono" />
            </div>
          </div>
        )}

        {settings.provider === "ollama" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.serverUrl")}</label>
              <input value={settings.ollama?.base_url || "http://localhost:11434"} onChange={(e) => updateOllama("base_url", e.target.value)} placeholder="http://localhost:11434" className="input-base font-mono" />
              <p className="text-xs text-muted-foreground mt-1">{t("ollama.serverUrlHint")}</p>
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.model")}</label>
              <input type="text" value={settings.ollama?.model || ""} onChange={(e) => updateOllama("model", e.target.value)} placeholder="llama3.1" className="input-base font-mono" />
            </div>
          </div>
        )}

        {settings.provider === "openrouter" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.apiKey")}</label>
              <input type="password" value={settings.openrouter?.api_key || ""} onChange={(e) => updateOpenRouter("api_key", e.target.value)} placeholder="sk-or-v1-..." className="input-base font-mono" />
              <p className="text-xs text-muted-foreground mt-1">
                {t("openrouter.keyHint")}{" "}
                <a href="https://openrouter.ai/keys" target="_blank" rel="noopener noreferrer" className="text-primary font-semibold hover:underline">openrouter.ai/keys</a>
              </p>
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.modelVision")}</label>
              <input
                type="text"
                value={settings.openrouter?.model || ""}
                onChange={(e) => updateOpenRouter("model", e.target.value)}
                placeholder="openai/gpt-4o"
                className="input-base font-mono"
              />
              <p className="text-xs text-muted-foreground mt-1.5">
                {t("openrouter.browseAt")}{" "}
                <a href="https://openrouter.ai/models" target="_blank" rel="noopener noreferrer" className="text-primary font-semibold hover:underline">openrouter.ai/models</a>
              </p>
            </div>
          </div>
        )}

        {settings.provider === "openai_compatible" && (
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.baseUrl")}</label>
              <input value={settings.openai_compatible?.base_url || ""} onChange={(e) => updateOpenAICompatible("base_url", e.target.value)} placeholder="https://api.together.xyz/v1" className="input-base font-mono" />
              <p className="text-xs text-muted-foreground mt-1">{t("openaiCompatible.baseUrlHint")}</p>
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.apiKey")} <span className="text-muted-foreground font-normal">({t("openaiCompatible.optional")})</span></label>
              <input type="password" value={settings.openai_compatible?.api_key || ""} onChange={(e) => updateOpenAICompatible("api_key", e.target.value)} placeholder="sk-..." className="input-base font-mono" />
              <p className="text-xs text-muted-foreground mt-1">{t("openaiCompatible.apiKeyHint")}</p>
            </div>
            <div>
              <label className="block text-sm font-semibold text-foreground mb-1.5">{t("labels.model")}</label>
              <input type="text" value={settings.openai_compatible?.model || ""} onChange={(e) => updateOpenAICompatible("model", e.target.value)} placeholder="meta-llama/Llama-3-70b-chat-hf" className="input-base font-mono" />
              <p className="text-xs text-muted-foreground mt-1">{t("openaiCompatible.modelHint")}</p>
            </div>
          </div>
        )}
      </div>

      {/* Status + Test */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2 h-5">
          {saving && <span className="flex items-center gap-1 text-xs text-muted-foreground"><Loader2 className="w-3 h-3 animate-spin" /> {t("status.saving")}</span>}
          {saved && !saving && <span className="flex items-center gap-1 text-xs text-success"><CheckCircle className="w-3 h-3" /> {t("status.saved")}</span>}
        </div>
        <button onClick={handleTest} disabled={testStatus === "testing"} className="btn-secondary disabled:opacity-50 ml-auto">
          {testStatus === "testing" ? <Loader2 className="w-4 h-4 animate-spin" /> : <TestTube className="w-4 h-4" />}
          {testStatus === "testing" ? t("status.testing") : t("status.testConnection")}
        </button>
      </div>
      {testStatus === "success" && <div className="flex items-center gap-2 text-success"><CheckCircle className="w-4 h-4" /><span className="text-sm">{testMessage}</span></div>}
      {testStatus === "error" && <div className="flex items-center gap-2 text-destructive"><XCircle className="w-4 h-4" /><span className="text-sm">{testMessage}</span></div>}
    </div>
  );
}
