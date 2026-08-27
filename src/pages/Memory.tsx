import { useState, useEffect } from "react";
import { Brain, Search, Trash2, Download, ChevronRight, RefreshCw, Globe, Loader2, Sparkles } from "lucide-react";
import { getMemoryStats, getMemoryDomains, getMemoriesForDomain, searchMemories, cleanupMemories, decayMemories, exportMemories, rerankCriticalMemories, getCriticalMemorySettings, updateCriticalMemorySettings } from "../lib/api";
import type { CriticalMemorySettings, DomainInfo, Memory as MemoryType, MemoryStats } from "../lib/types";
import { PageHeader, LoadingSpinner } from "../components/ui";
import { useTranslation } from "react-i18next";

const CATEGORY_COLORS: Record<string, string> = {
  navigation: "bg-primary/10 text-primary",
  form_strategy: "bg-accent text-accent-foreground",
  element_interaction: "bg-warning/12 text-warning",
  failure_recovery: "bg-destructive/10 text-destructive",
  site_structure: "bg-success/12 text-success",
  qa_pattern: "bg-secondary text-foreground",
};

export default function Memory() {
  const { t } = useTranslation("memory");
  const [stats, setStats] = useState<MemoryStats>({ total_memories: 0, unique_domains: 0, by_category: {} });
  const [domains, setDomains] = useState<DomainInfo[]>([]);
  const [selectedDomain, setSelectedDomain] = useState<string | null>(null);
  const [memories, setMemories] = useState<MemoryType[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<MemoryType[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMemories, setLoadingMemories] = useState(false);
  const [reranking, setReranking] = useState(false);
  const [rerankMessage, setRerankMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const [criticalSettings, setCriticalSettings] = useState<CriticalMemorySettings>({ auto_rerank: true, max_count: 5, max_tokens: 500 });
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [policyMessage, setPolicyMessage] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      getMemoryStats(),
      getMemoryDomains(),
      getCriticalMemorySettings(),
    ])
      .then(([s, d, policy]) => {
        setStats(s);
        setDomains(Array.isArray(d) ? (d as DomainInfo[]) : []);
        setCriticalSettings(policy);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const selectDomain = (domain: string) => {
    setSelectedDomain(domain);
    setSearchResults(null);
    setLoadingMemories(true);
    getMemoriesForDomain(domain)
      .then((data) => setMemories((data as MemoryType[]) || []))
      .catch(() => setMemories([]))
      .finally(() => setLoadingMemories(false));
  };

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    if (!searchQuery.trim()) { setSearchResults(null); return; }
    setSelectedDomain(null);
    setLoadingMemories(true);
    searchMemories(searchQuery)
      .then((data) => setSearchResults((data as MemoryType[]) || []))
      .catch(() => setSearchResults([]))
      .finally(() => setLoadingMemories(false));
  };

  const handleDecay = async () => {
    if (!confirm(t("confirm.decay"))) return;
    const result = await decayMemories(30);
    alert(t("toast.decayed", { count: result.affected }));
  };

  const handleCleanup = async () => {
    if (!confirm(t("confirm.cleanup"))) return;
    const result = await cleanupMemories();
    alert(t("toast.deleted", { count: result.deleted }));
  };

  const handleExport = async () => {
    const data = await exportMemories();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "memory_export.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleRerank = async () => {
    setReranking(true);
    setRerankMessage(null);
    try {
      const result = await rerankCriticalMemories();
      const [nextStats, nextDomains] = await Promise.all([getMemoryStats(), getMemoryDomains()]);
      setStats(nextStats);
      setDomains(Array.isArray(nextDomains) ? nextDomains : []);
      if (selectedDomain) {
        const nextMemories = await getMemoriesForDomain(selectedDomain);
        setMemories(Array.isArray(nextMemories) ? nextMemories : []);
      }
      setRerankMessage({
        kind: "success",
        text: t("rerank.success", { count: result.selected, scopes: result.scopes }),
      });
    } catch (error) {
      setRerankMessage({
        kind: "error",
        text: error instanceof Error ? error.message : t("rerank.error"),
      });
    } finally {
      setReranking(false);
    }
  };

  const handlePolicySave = async () => {
    setSavingPolicy(true);
    setPolicyMessage(null);
    try {
      const saved = await updateCriticalMemorySettings(criticalSettings);
      setCriticalSettings({
        auto_rerank: saved.auto_rerank,
        max_count: saved.max_count,
        max_tokens: saved.max_tokens,
      });
      setStats((current) => ({
        ...current,
        critical_max_count: saved.max_count,
        critical_max_tokens: saved.max_tokens,
      }));
      if (selectedDomain) {
        const nextMemories = await getMemoriesForDomain(selectedDomain);
        setMemories(Array.isArray(nextMemories) ? nextMemories : []);
      }
      setPolicyMessage(t("policy.saved"));
    } catch (error) {
      setPolicyMessage(error instanceof Error ? error.message : t("policy.error"));
    } finally {
      setSavingPolicy(false);
    }
  };

  const displayMemories = [...(searchResults !== null ? searchResults : memories)].sort((a, b) => {
    if (Boolean(a.critical) !== Boolean(b.critical)) return a.critical ? -1 : 1;
    return (a.critical_rank ?? Number.MAX_SAFE_INTEGER) - (b.critical_rank ?? Number.MAX_SAFE_INTEGER);
  });
  const criticalMemories = displayMemories.filter((memory) => memory.critical);
  const criticalTokens = criticalMemories.reduce((total, memory) => total + (memory.estimated_tokens || 0), 0);
  const criticalMaxCount = stats.critical_max_count || 5;
  const criticalMaxTokens = stats.critical_max_tokens || 500;
  const policyValid = criticalSettings.max_count >= 1
    && criticalSettings.max_count <= 20
    && criticalSettings.max_tokens >= 100
    && criticalSettings.max_tokens <= 4000;

  if (loading) return <LoadingSpinner />;

  return (
    <div>
      <PageHeader
        title={t("title")}
        subtitle={t("subtitle", { total: stats.total_memories, domains: stats.unique_domains })}
        actions={
          <>
            <button onClick={handleRerank} disabled={reranking} className="btn-primary">
              {reranking ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
              {reranking ? t("actions.reranking") : t("actions.rerank")}
            </button>
            <button onClick={handleExport} className="btn-secondary"><Download className="w-4 h-4" /> {t("actions.export")}</button>
            <button onClick={handleDecay} className="btn-secondary"><RefreshCw className="w-4 h-4" /> {t("actions.decay")}</button>
            <button onClick={handleCleanup} className="btn-destructive"><Trash2 className="w-4 h-4" /> {t("actions.cleanup")}</button>
          </>
        }
      />

      {rerankMessage && (
        <div className={`mb-4 rounded-lg border px-3 py-2 text-sm ${rerankMessage.kind === "success" ? "border-success/30 bg-success/10 text-success" : "border-destructive/30 bg-destructive/10 text-destructive"}`}>
          {rerankMessage.text}
        </div>
      )}

      <section className="mb-4 rounded-xl border border-border bg-card px-4 py-3" aria-labelledby="critical-policy-title">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <h2 id="critical-policy-title" className="text-sm font-semibold text-foreground">{t("policy.title")}</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">{t("policy.description")}</p>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex items-center gap-2 pb-1 text-xs font-medium text-foreground">
              <button
                type="button"
                role="switch"
                aria-checked={criticalSettings.auto_rerank}
                onClick={() => setCriticalSettings((current) => ({ ...current, auto_rerank: !current.auto_rerank }))}
                className={`relative h-5 w-9 rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-primary/30 ${criticalSettings.auto_rerank ? "bg-primary" : "bg-border"}`}
              >
                <span className={`absolute left-0 top-0.5 h-4 w-4 rounded-full bg-card transition-transform ${criticalSettings.auto_rerank ? "translate-x-4" : "translate-x-0.5"}`} />
              </button>
              {t("policy.auto")}
            </label>
            <label className="text-[11px] font-medium text-muted-foreground">
              {t("policy.maxMemories")}
              <input
                type="number"
                min={1}
                max={20}
                value={criticalSettings.max_count}
                onChange={(event) => setCriticalSettings((current) => ({ ...current, max_count: Number(event.target.value) }))}
                className="mt-1 block w-24 rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/20"
              />
            </label>
            <label className="text-[11px] font-medium text-muted-foreground">
              {t("policy.maxTokens")}
              <input
                type="number"
                min={100}
                max={4000}
                step={100}
                value={criticalSettings.max_tokens}
                onChange={(event) => setCriticalSettings((current) => ({ ...current, max_tokens: Number(event.target.value) }))}
                className="mt-1 block w-28 rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/20"
              />
            </label>
            <button onClick={handlePolicySave} disabled={!policyValid || savingPolicy} className="btn-secondary">
              {savingPolicy && <Loader2 className="h-4 w-4 animate-spin" />}
              {t("policy.save")}
            </button>
          </div>
        </div>
        {policyMessage && <p className="mt-2 text-xs text-muted-foreground">{policyMessage}</p>}
      </section>

      {/* Category stats */}
      {Object.keys(stats.by_category).length > 0 && (
        <div className="flex flex-wrap gap-2 mb-4">
          {Object.entries(stats.by_category).map(([cat, count]) => (
            <span key={cat} className={`px-2.5 py-1 rounded-full text-xs font-medium ${CATEGORY_COLORS[cat] || "bg-secondary text-foreground"}`}>
              {cat.replace("_", " ")}: {count}
            </span>
          ))}
        </div>
      )}

      {/* Search */}
      <form onSubmit={handleSearch} className="card mb-5">
        <div className="flex items-center gap-3">
          <div className="flex-1 relative">
            <Search className="w-4 h-4 absolute left-3 top-2.5 text-muted-foreground" />
            <input value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)}
              placeholder={t("search.placeholder")} className="w-full pl-9 pr-3 py-2 border border-border rounded-lg text-sm" />
          </div>
          <button type="submit" className="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium">{t("search.button")}</button>
        </div>
      </form>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* Domain List */}
        <div className="overflow-hidden rounded-xl border border-border bg-card lg:col-span-1">
          <div className="p-3 border-b border-border">
            <h3 className="text-sm font-bold text-foreground">{t("domains.title")}</h3>
          </div>
          {domains.length === 0 ? (
            <div className="p-4 text-center">
              <Globe className="w-8 h-8 text-muted-foreground mx-auto mb-2" />
              <p className="text-xs text-muted-foreground">{t("domains.empty")}</p>
            </div>
          ) : (
            <div className="divide-y divide-border max-h-[500px] overflow-y-auto">
              {domains.map((d) => (
                <button
                  key={d.website_domain}
                  onClick={() => selectDomain(d.website_domain)}
                  className={`w-full text-left p-3 hover:bg-secondary transition-colors flex items-center justify-between ${
                    selectedDomain === d.website_domain ? "bg-primary/5 border-l-2 border-l-primary" : ""
                  }`}
                >
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-foreground truncate">{d.website_domain}</p>
                    <p className="text-[10px] text-muted-foreground">
                      {d.ats_platform || "—"} · {d.count} memories · {Math.round(d.avg_confidence * 100)}% conf
                    </p>
                  </div>
                  <ChevronRight className="w-4 h-4 text-muted-foreground flex-shrink-0" />
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Memory List */}
        <div className="overflow-hidden rounded-xl border border-border bg-card lg:col-span-2">
          <div className="p-3 border-b border-border">
            <h3 className="text-sm font-bold text-foreground">
              {searchResults !== null ? t("memories.searchResults", { count: searchResults.length }) :
               selectedDomain ? t("memories.domainTitle", { domain: selectedDomain, count: memories.length }) : t("memories.selectDomain")}
            </h3>
          </div>
          {selectedDomain && searchResults === null && (
            <div className="border-b border-border bg-secondary/40 px-3 py-2.5">
              <div className="flex items-center justify-between gap-3 text-xs">
                <div>
                  <span className="font-semibold text-foreground">{t("critical.title")}</span>
                  <span className="ml-2 text-muted-foreground">{t("critical.description")}</span>
                </div>
                <span className="shrink-0 font-mono text-muted-foreground">
                  {t("critical.budget", { count: criticalMemories.length, maxCount: criticalMaxCount, tokens: criticalTokens, maxTokens: criticalMaxTokens })}
                </span>
              </div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
                <div
                  className="h-full rounded-full bg-primary transition-all"
                  style={{ width: `${Math.min(100, Math.max(criticalMemories.length / criticalMaxCount, criticalTokens / criticalMaxTokens) * 100)}%` }}
                />
              </div>
            </div>
          )}
          {loadingMemories ? (
            <div className="flex items-center justify-center h-48">
              <Loader2 className="w-6 h-6 animate-spin text-primary" />
            </div>
          ) : displayMemories.length === 0 ? (
            <div className="p-8 text-center">
              <Brain className="w-10 h-10 text-muted-foreground mx-auto mb-2" />
              <p className="text-sm text-muted-foreground">
                {selectedDomain || searchResults !== null ? t("memories.noMemoriesFound") : t("memories.selectDomainPrompt")}
              </p>
            </div>
          ) : (
            <div className="divide-y divide-border max-h-[500px] overflow-y-auto">
              {displayMemories.map((m) => (
                <div key={m.id} className="p-3">
                  <div className="flex items-start justify-between gap-2">
                    <p className="text-sm text-foreground flex-1">{m.content}</p>
                    <div className="flex items-center gap-1 flex-shrink-0">
                      {m.critical && (
                        <span className="px-1.5 py-0.5 rounded bg-primary/10 text-primary text-[10px] font-semibold">
                          {t("critical.rank", { rank: m.critical_rank })}
                        </span>
                      )}
                      {m.agent_marked_critical && !m.critical && (
                        <span className="px-1.5 py-0.5 rounded bg-warning/12 text-warning text-[10px] font-medium">
                          {t("critical.nominated")}
                        </span>
                      )}
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${CATEGORY_COLORS[m.category] || "bg-secondary text-foreground"}`}>
                        {m.category.replace("_", " ")}
                      </span>
                    </div>
                  </div>
                  {m.critical && m.critical_reason && (
                    <p className="mt-1.5 text-xs text-muted-foreground">{m.critical_reason}</p>
                  )}
                  <div className="flex items-center gap-3 mt-1.5">
                    <span className="text-[10px] text-muted-foreground">{m.website_domain}</span>
                    <span className={`text-[10px] ${m.success ? "text-success" : "text-destructive"}`}>
                      {m.success ? t("memories.success") : t("memories.failure")}
                    </span>
                    <span className="text-[10px] text-muted-foreground">
                      {t("memories.confidence", { percent: Math.round(m.confidence * 100) })}
                    </span>
                    <span className="text-[10px] text-muted-foreground">
                      {t("memories.accesses", { count: m.access_count })}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
