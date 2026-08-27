import { useEffect, useRef, useState } from "react";
import { NavLink } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  BarChart3,
  BookOpen,
  Bot,
  Brain,
  BriefcaseBusiness,
  CircleHelp,
  FileQuestion,
  Gauge,
  ScrollText,
  Settings,
  UserRound,
} from "lucide-react";
import { checkHealth } from "../../lib/api";

type NavigationItem = {
  to: string;
  icon: typeof Gauge;
  labelKey: string;
  fallback: string;
};

const navigationGroups: Array<{ label: string; items: NavigationItem[] }> = [
  {
    label: "Workspace",
    items: [
      { to: "/", icon: Gauge, labelKey: "nav.overview", fallback: "Overview" },
      { to: "/jobs", icon: Bot, labelKey: "nav.runner", fallback: "Runner & jobs" },
    ],
  },
  {
    label: "Application kit",
    items: [
      { to: "/profile", icon: UserRound, labelKey: "nav.profile", fallback: "Profile" },
      { to: "/qa", icon: FileQuestion, labelKey: "nav.qa", fallback: "Answers" },
      { to: "/memory", icon: Brain, labelKey: "nav.memory", fallback: "Learned patterns" },
    ],
  },
  {
    label: "Review",
    items: [
      { to: "/analytics", icon: BarChart3, labelKey: "nav.analytics", fallback: "Insights" },
      { to: "/logs", icon: ScrollText, labelKey: "nav.logs", fallback: "Activity" },
    ],
  },
  {
    label: "System",
    items: [
      { to: "/settings", icon: Settings, labelKey: "nav.settings", fallback: "Settings" },
    ],
  },
];

const mobileItems = [
  navigationGroups[0].items[0],
  navigationGroups[0].items[1],
  navigationGroups[1].items[0],
  navigationGroups[3].items[0],
];

function NavItem({ item, compact = false }: { item: NavigationItem; compact?: boolean }) {
  const { t } = useTranslation("common");
  const Icon = item.icon;

  return (
    <NavLink
      to={item.to}
      end={item.to === "/"}
      aria-label={compact ? t(item.labelKey, item.fallback) : undefined}
      className={({ isActive }) =>
        compact
          ? `flex h-10 w-10 items-center justify-center rounded-lg transition-colors ${
              isActive ? "bg-primary/12 text-primary" : "text-muted-foreground hover:bg-secondary hover:text-foreground"
            }`
          : `group relative flex items-center gap-3 rounded-lg px-3 py-2 text-[13px] font-medium transition-colors ${
              isActive
                ? "bg-primary/10 text-primary"
                : "text-muted-foreground hover:bg-secondary hover:text-foreground"
            }`
      }
    >
      <Icon className="h-[17px] w-[17px] shrink-0" />
      {!compact && <span>{t(item.labelKey, item.fallback)}</span>}
    </NavLink>
  );
}

export default function Sidebar() {
  const { t } = useTranslation("common");
  const [backendOk, setBackendOk] = useState<boolean | null>(null);
  const [buildId, setBuildId] = useState("");
  const backendOkRef = useRef<boolean | null>(null);

  useEffect(() => {
    let interval: ReturnType<typeof setTimeout> | null = null;

    const check = () => {
      checkHealth()
        .then((data) => {
          backendOkRef.current = true;
          setBackendOk(true);
          setBuildId(data.build_id || "");
        })
        .catch(() => {
          backendOkRef.current = false;
          setBackendOk(false);
        });
    };
    check();

    const scheduleNext = () => {
      interval = setTimeout(() => {
        check();
        scheduleNext();
      }, backendOkRef.current ? 15000 : 2000);
    };
    scheduleNext();

    return () => {
      if (interval) clearTimeout(interval);
    };
  }, []);

  const connectionLabel = backendOk === true
    ? `${t("status.connected")}${buildId ? ` · ${buildId}` : ""}`
    : backendOk === false
      ? t("status.startingBackend")
      : t("status.connecting");

  return (
    <>
      <aside className="fixed inset-y-0 left-0 z-40 hidden w-64 flex-col border-r border-border bg-card lg:flex">
        <div className="px-5 pb-6 pt-6">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-foreground text-background">
              <BriefcaseBusiness className="h-[18px] w-[18px]" />
            </div>
            <div className="min-w-0">
              <h1 className="text-[15px] font-semibold leading-tight tracking-[-0.02em] text-foreground">
                {t("app.title")}
              </h1>
              <p className="mt-0.5 truncate text-[11px] text-muted-foreground">Autonomous job search</p>
            </div>
          </div>
        </div>

        <nav className="scrollbar-hidden flex-1 overflow-y-auto px-3 pb-4">
          {navigationGroups.map((group, index) => (
            <div key={group.label} className={index === 0 ? "" : "mt-6"}>
              <p className="mb-1.5 px-3 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground/75">
                {group.label}
              </p>
              <div className="space-y-0.5">
                {group.items.map((item) => <NavItem key={item.to} item={item} />)}
              </div>
            </div>
          ))}
        </nav>

        <div className="border-t border-border px-3 py-3">
          <div className="mb-2">
            <NavLink to="/guide" className="flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium text-muted-foreground hover:bg-secondary hover:text-foreground">
              <CircleHelp className="h-4 w-4" /> Help
            </NavLink>
          </div>
          <div className="flex items-center gap-2 rounded-lg bg-secondary/65 px-3 py-2.5" title={connectionLabel}>
            <span className={`h-2 w-2 shrink-0 rounded-full ${
              backendOk === true ? "bg-success" : backendOk === false ? "bg-destructive" : "bg-muted-foreground"
            }`} />
            <span className="truncate text-[11px] text-muted-foreground">{connectionLabel}</span>
          </div>
        </div>
      </aside>

      <header className="fixed inset-x-0 top-0 z-40 flex h-[72px] items-center justify-between border-b border-border bg-card/95 px-4 backdrop-blur lg:hidden">
        <NavLink to="/" className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-foreground text-background">
            <BriefcaseBusiness className="h-4 w-4" />
          </span>
          <span className="text-sm font-semibold text-foreground">{t("app.title")}</span>
        </NavLink>
        <nav className="flex items-center gap-1" aria-label="Primary navigation">
          {mobileItems.map((item) => <NavItem key={item.to} item={item} compact />)}
          <NavLink to="/guide" aria-label="Help" className="flex h-10 w-10 items-center justify-center rounded-lg text-muted-foreground hover:bg-secondary hover:text-foreground">
            <BookOpen className="h-[17px] w-[17px]" />
          </NavLink>
        </nav>
      </header>
    </>
  );
}
