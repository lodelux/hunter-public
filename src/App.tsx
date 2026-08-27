import { useState, useEffect, useCallback, lazy, Suspense } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import Sidebar from "./components/layout/Sidebar";
import ErrorBoundary from "./components/ErrorBoundary";
import SetupWizard from "./components/SetupWizard";
import { getSetupStatus } from "./lib/api";
import { useDirection } from "./i18n/useDirection";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import { Wand2, Loader2 } from "lucide-react";

const Dashboard = lazy(() => import("./pages/Dashboard"));
const Analytics = lazy(() => import("./pages/Analytics"));
const Profile = lazy(() => import("./pages/Profile"));
const LLMSettings = lazy(() => import("./pages/LLMSettings"));
const Jobs = lazy(() => import("./pages/Jobs"));
const Memory = lazy(() => import("./pages/Memory"));
const SettingsPage = lazy(() => import("./pages/Settings"));
const Logs = lazy(() => import("./pages/Logs"));
const Guide = lazy(() => import("./pages/Guide"));
const QA = lazy(() => import("./pages/QA"));

function PageLoader() {
  return (
    <div className="flex items-center justify-center h-64">
      <Loader2 className="w-8 h-8 animate-spin text-primary" />
    </div>
  );
}

export default function App() {
  useDirection();
  useKeyboardShortcuts();
  const [showWizard, setShowWizard] = useState(false);
  const [wizardChecked, setWizardChecked] = useState(false);
  const [wizardPaused, setWizardPaused] = useState(false);
  const [wizardStep, setWizardStep] = useState(0);
  useEffect(() => {
    let cancelled = false;
    async function check() {
      for (let i = 0; i < 15; i++) {
        try {
          const status = await getSetupStatus();
          if (!cancelled) {
            if (!status.onboarding_completed && !status.all_required_done) {
              setShowWizard(true);
            }
            setWizardChecked(true);
          }
          return;
        } catch {
          await new Promise((r) => setTimeout(r, 1500));
        }
      }
      if (!cancelled) setWizardChecked(true);
    }
    check();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const handler = () => {
      setWizardStep(0);
      setWizardPaused(false);
      setShowWizard(true);
    };
    window.addEventListener("open-setup-wizard", handler);
    return () => window.removeEventListener("open-setup-wizard", handler);
  }, []);

  const handleWizardNavigateAway = useCallback((currentStep: number) => {
    setWizardStep(currentStep);
    setShowWizard(false);
    setWizardPaused(true);
  }, []);

  const handleResumeWizard = useCallback(() => {
    setWizardPaused(false);
    setShowWizard(true);
  }, []);

  const handleWizardClose = useCallback(() => {
    setShowWizard(false);
    setWizardPaused(false);
    setWizardStep(0);
  }, []);

  return (
    <div className="min-h-screen bg-background">
      <Sidebar />
      <main className="min-w-0 px-4 pb-10 pt-24 sm:px-6 lg:ml-64 lg:px-10 lg:py-8">
        <ErrorBoundary>
          <Suspense fallback={<PageLoader />}>
            <div className="page-shell">
              <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/analytics" element={<Analytics />} />
                <Route path="/profile" element={<Profile />} />
                <Route path="/llm" element={<LLMSettings />} />
                <Route path="/jobs" element={<Jobs />} />
                <Route path="/apply" element={<Navigate to="/jobs?tab=pending" replace />} />
                <Route path="/memory" element={<Memory />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="/logs" element={<Logs />} />
                <Route path="/guide" element={<Guide />} />
                <Route path="/qa" element={<QA />} />
              </Routes>
            </div>
          </Suspense>
        </ErrorBoundary>
      </main>

      {wizardChecked && showWizard && (
        <SetupWizard
          onClose={handleWizardClose}
          onNavigateAway={handleWizardNavigateAway}
          initialStep={wizardStep}
        />
      )}

      {wizardPaused && !showWizard && (
        <button
          onClick={handleResumeWizard}
          className="fixed bottom-6 right-6 z-40 flex items-center gap-2 px-4 py-3 bg-primary text-primary-foreground rounded-xl shadow-lg hover:bg-primary/90 transition-all hover:scale-105 text-sm font-medium"
        >
          <Wand2 className="w-4 h-4" />
          Resume Setup Wizard
        </button>
      )}
    </div>
  );
}
