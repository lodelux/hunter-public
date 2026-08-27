import { useEffect, useState } from "react";
import { CheckCircle, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { getCountries, getProfile, saveProfile } from "../lib/api";
import type { CandidateProfile, CountryConfig } from "../lib/types";
import { LoadingSpinner, PageHeader, Section } from "../components/ui";
import TagInput from "../components/ui/TagInput";
import { loadLanguage } from "../i18n";
import {
  getLanguageFromCountry,
  getSavedLanguage,
} from "../i18n/languageDetection";
import { useAutosave } from "../hooks/useAutosave";

const defaultProfile: CandidateProfile = {
  country: "US",
  target_job_titles: [],
  target_locations: [],
  blacklisted_companies: ["Alignerr", "Outlier"],
  languages: ["English"],
  visa_sponsorship_needed: false,
  salary_expectation: {
    min: 50000,
    currency: "USD",
    period: "annual",
  },
  markdown: "",
};

type ProfileListField =
  | "target_job_titles"
  | "target_locations"
  | "blacklisted_companies"
  | "languages";

export default function Profile() {
  const { t } = useTranslation("profile");
  const [profile, setProfile] = useState(defaultProfile);
  const [countries, setCountries] = useState<Record<string, CountryConfig>>({});
  const [newTitle, setNewTitle] = useState("");
  const [newLocation, setNewLocation] = useState("");
  const [newBlacklistedCompany, setNewBlacklistedCompany] = useState("");
  const [newLanguage, setNewLanguage] = useState("");
  const [loading, setLoading] = useState(true);
  const autosave = useAutosave(profile, saveProfile, !loading);

  useEffect(() => {
    Promise.all([
      getProfile().then((data) =>
        setProfile({
          ...defaultProfile,
          ...data,
          salary_expectation: {
            ...defaultProfile.salary_expectation,
            ...data.salary_expectation,
          },
        }),
      ),
      getCountries().then((data) => setCountries(data.countries)),
    ])
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const update = <K extends keyof CandidateProfile>(
    field: K,
    value: CandidateProfile[K],
  ) => {
    setProfile((current) => ({ ...current, [field]: value }));
  };

  const updateSalary = (
    field: keyof CandidateProfile["salary_expectation"],
    value: string | number,
  ) => {
    setProfile((current) => ({
      ...current,
      salary_expectation: {
        ...current.salary_expectation,
        [field]: value,
      },
    }));
  };

  const handleCountryChange = (country: string) => {
    const config = countries[country];
    setProfile((current) => ({
      ...current,
      country,
      salary_expectation: config
        ? {
            ...current.salary_expectation,
            currency: config.currency,
            period: config.salary_period as "annual" | "monthly",
          }
        : current.salary_expectation,
    }));
    if (config && !getSavedLanguage()) {
      loadLanguage(getLanguageFromCountry(country));
    }
  };

  const addToList = (
    field: ProfileListField,
    value: string,
    clear: () => void,
  ) => {
    const cleaned = value.trim();
    if (cleaned && !profile[field].includes(cleaned)) {
      update(field, [...profile[field], cleaned]);
    }
    clear();
  };

  const removeFromList = (field: ProfileListField, value: string) => {
    update(
      field,
      profile[field].filter((item) => item !== value),
    );
  };

  if (loading) return <LoadingSpinner />;

  return (
    <div className="max-w-[1280px]">
      <PageHeader
        title={t("title")}
        subtitle="Keep deterministic search facts structured and write everything else freely in Markdown."
        actions={
          <span className="flex items-center gap-1.5 text-sm text-muted-foreground" role="status" aria-live="polite">
            {autosave.status === "pending" || autosave.status === "saving" ? (
              <><Loader2 className="h-4 w-4 animate-spin" /> Saving…</>
            ) : autosave.status === "saved" ? (
              <><CheckCircle className="h-4 w-4 text-success" /> {t("saved")}</>
            ) : (
              "Changes save automatically"
            )}
          </span>
        }
      />

      {autosave.status === "error" && (
        <div className="error-banner mb-5 flex items-center justify-between gap-3">
          {t("saveError")}
          <button type="button" onClick={autosave.retry} className="btn-secondary">Retry</button>
        </div>
      )}

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(360px,0.75fr)_minmax(0,1.25fr)]">
      <Section title="Search & screening facts" className="">
        <p className="mb-5 text-sm text-muted-foreground">
          These values drive collection and deterministic screening. They are
          kept separate from the Markdown profile.
        </p>

        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block">
            <span className="mb-1.5 block text-sm font-semibold text-foreground">
              {t("country.label")}
            </span>
            <select
              value={profile.country}
              onChange={(event) => handleCountryChange(event.target.value)}
              className="w-full rounded-lg border border-border bg-card px-3 py-2 text-sm"
            >
              <option value="">{t("country.selectPlaceholder")}</option>
              {Object.entries(countries).map(([code, config]) => (
                <option key={code} value={code}>
                  {config.flag} {config.name}
                </option>
              ))}
            </select>
          </label>

          <label className="flex items-end gap-2.5 pb-2 cursor-pointer">
            <input
              type="checkbox"
              checked={profile.visa_sponsorship_needed}
              onChange={(event) =>
                update("visa_sponsorship_needed", event.target.checked)
              }
              className="h-4 w-4 rounded-md border-border accent-primary"
            />
            <span className="text-sm text-foreground">
              {t("work.visaSponsorship")}
            </span>
          </label>
        </div>

        <div className="mt-5 grid gap-4 sm:grid-cols-3">
          <label className="block">
            <span className="mb-1.5 block text-sm font-semibold text-foreground">
              {t("salary.minimum")}
            </span>
            <input
              type="number"
              value={profile.salary_expectation.min}
              onChange={(event) =>
                updateSalary("min", Number(event.target.value))
              }
              className="input-base"
            />
          </label>
          <label className="block">
            <span className="mb-1.5 block text-sm font-semibold text-foreground">
              {t("salary.currency")}
            </span>
            <input
              value={profile.salary_expectation.currency}
              onChange={(event) =>
                updateSalary("currency", event.target.value)
              }
              className="input-base"
            />
          </label>
          <label className="block">
            <span className="mb-1.5 block text-sm font-semibold text-foreground">
              {t("salary.period")}
            </span>
            <select
              value={profile.salary_expectation.period}
              onChange={(event) =>
                updateSalary("period", event.target.value)
              }
              className="w-full rounded-lg border border-border bg-card px-3 py-2 text-sm"
            >
              <option value="annual">{t("salary.annual")}</option>
              <option value="monthly">{t("salary.monthly")}</option>
            </select>
          </label>
        </div>

        <div className="mt-6 grid gap-6">
          <div>
            <h3 className="mb-2 text-sm font-semibold text-foreground">
              {t("targetJobTitles.title")}
            </h3>
            <TagInput
              tags={profile.target_job_titles}
              value={newTitle}
              onChange={setNewTitle}
              onAdd={() =>
                addToList("target_job_titles", newTitle, () => setNewTitle(""))
              }
              onRemove={(value) => removeFromList("target_job_titles", value)}
              placeholder={t("targetJobTitles.placeholder")}
            />
          </div>
          <div>
            <h3 className="mb-2 text-sm font-semibold text-foreground">
              {t("targetLocations.title")}
            </h3>
            <TagInput
              tags={profile.target_locations}
              value={newLocation}
              onChange={setNewLocation}
              onAdd={() =>
                addToList("target_locations", newLocation, () =>
                  setNewLocation(""),
                )
              }
              onRemove={(value) => removeFromList("target_locations", value)}
              placeholder={t("targetLocations.placeholder")}
            />
          </div>
          <div>
            <h3 className="mb-2 text-sm font-semibold text-foreground">
              {t("languages.title")}
            </h3>
            <TagInput
              tags={profile.languages}
              value={newLanguage}
              onChange={setNewLanguage}
              onAdd={() =>
                addToList("languages", newLanguage, () => setNewLanguage(""))
              }
              onRemove={(value) => removeFromList("languages", value)}
              placeholder={t("languages.placeholder")}
            />
          </div>
          <div>
            <h3 className="mb-2 text-sm font-semibold text-foreground">
              {t("blacklistedCompanies.title")}
            </h3>
            <p className="mb-2 text-xs text-muted-foreground">
              {t("blacklistedCompanies.description")}
            </p>
            <TagInput
              tags={profile.blacklisted_companies}
              value={newBlacklistedCompany}
              onChange={setNewBlacklistedCompany}
              onAdd={() =>
                addToList(
                  "blacklisted_companies",
                  newBlacklistedCompany,
                  () => setNewBlacklistedCompany(""),
                )
              }
              onRemove={(value) =>
                removeFromList("blacklisted_companies", value)
              }
              placeholder={t("blacklistedCompanies.placeholder")}
            />
          </div>
        </div>
      </Section>

      <Section title="Profile.md" className="">
        <p className="mb-3 text-sm text-muted-foreground">
          This document is sent directly to the applier, classifier, resume
          tailor, and cover-letter generator. Keep the premade headers or adapt
          them to your needs. Put tone and content preferences under{" "}
          <code>## Cover Letter Instructions</code>; Hunter generates the actual
          letter separately for each job.
        </p>
        <textarea
          aria-label="Markdown profile"
          value={profile.markdown}
          onChange={(event) => update("markdown", event.target.value)}
          rows={30}
          spellCheck
          className="input-base min-h-[36rem] resize-y font-mono text-[13px] leading-6"
        />
      </Section>
      </div>
    </div>
  );
}
