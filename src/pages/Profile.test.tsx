import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Profile from "./Profile";
import { getCountries, getProfile, saveProfile } from "../lib/api";
import type { CandidateProfile, CountryConfig } from "../lib/types";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { changeLanguage: vi.fn() },
  }),
}));

vi.mock("../i18n", () => ({ loadLanguage: vi.fn() }));
vi.mock("../i18n/languageDetection", () => ({
  getLanguageFromCountry: vi.fn(() => "en"),
  getSavedLanguage: vi.fn(() => null),
}));

vi.mock("../lib/api", () => ({
  getProfile: vi.fn(),
  saveProfile: vi.fn(),
  getCountries: vi.fn(),
}));

const mockGetProfile = vi.mocked(getProfile);
const mockSaveProfile = vi.mocked(saveProfile);
const mockGetCountries = vi.mocked(getCountries);

const usConfig: CountryConfig = {
  name: "United States",
  flag: "🇺🇸",
  date_format: "MM/DD/YYYY",
  currency: "USD",
  salary_period: "annual",
  address_labels: { state: "State", zip: "ZIP" },
  work_auth_options: ["Citizen"],
  show_notice_period: false,
  show_nationality: false,
  show_cover_letter: false,
  show_photo: false,
  show_date_of_birth: false,
  phone_prefix: "+1",
  default_sources: ["linkedin"],
};

const deConfig: CountryConfig = {
  ...usConfig,
  name: "Germany",
  flag: "🇩🇪",
  currency: "EUR",
  phone_prefix: "+49",
};

const sampleProfile: CandidateProfile = {
  country: "US",
  target_job_titles: ["Backend Developer"],
  target_locations: ["Remote"],
  blacklisted_companies: ["Alignerr", "Outlier"],
  languages: ["English"],
  visa_sponsorship_needed: false,
  salary_expectation: {
    min: 70000,
    currency: "USD",
    period: "annual",
  },
  markdown: "# Personal Details\nName: Ada Lovelace\n\n# Skills\n- Python\n",
};

describe("Profile page", () => {
  beforeEach(() => {
    mockGetProfile.mockResolvedValue(sampleProfile);
    mockGetCountries.mockResolvedValue({
      success: true,
      countries: { US: usConfig, DE: deConfig },
      notice_period_options: [],
    } as never);
    mockSaveProfile.mockResolvedValue({ success: true } as never);
  });

  it("loads the fixed settings and Markdown editor", async () => {
    render(<Profile />);
    expect(screen.queryByText("Changes save automatically")).not.toBeInTheDocument();

    expect(await screen.findByText("Changes save automatically")).toBeInTheDocument();
    expect(screen.getByText("Backend Developer")).toBeInTheDocument();
    expect(screen.getByText("Remote")).toBeInTheDocument();
    expect(screen.getByText("Alignerr")).toBeInTheDocument();
    expect(screen.getByLabelText("Markdown profile")).toHaveValue(
      sampleProfile.markdown,
    );
  });

  it("shows only deterministic profile controls outside the Markdown editor", async () => {
    render(<Profile />);
    await screen.findByLabelText("Markdown profile");

    expect(screen.getByText("Search & screening facts")).toBeInTheDocument();
    expect(screen.getByText("Profile.md")).toBeInTheDocument();
    expect(screen.queryByText("personal.fullName")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("skills.placeholder")).not.toBeInTheDocument();
  });

  it("autosaves Markdown edits with the fixed values", async () => {
    const user = userEvent.setup();
    render(<Profile />);
    const editor = await screen.findByLabelText("Markdown profile");

    await user.clear(editor);
    await user.type(editor, "# Personal Details\nName: Grace Hopper");

    await waitFor(() =>
      expect(mockSaveProfile).toHaveBeenCalledWith(
        expect.objectContaining({
          country: "US",
          markdown: "# Personal Details\nName: Grace Hopper",
        }),
      ),
    );
    expect(await screen.findByText("saved")).toBeInTheDocument();
  });

  it("adds and removes a fixed target title", async () => {
    const user = userEvent.setup();
    render(<Profile />);
    await screen.findByLabelText("Markdown profile");

    const input = screen.getByPlaceholderText("targetJobTitles.placeholder");
    await user.type(input, "Security Engineer{Enter}");
    expect(await screen.findByText("Security Engineer")).toBeInTheDocument();

    const existing = screen.getByText("Backend Developer").closest("span")!;
    await user.click(within(existing).getByRole("button"));

    await waitFor(() => {
      const payload = mockSaveProfile.mock.calls.at(-1)![0];
      expect(payload.target_job_titles).toEqual(["Security Engineer"]);
    });
  });

  it("edits the company blacklist", async () => {
    const user = userEvent.setup();
    render(<Profile />);
    await screen.findByLabelText("Markdown profile");

    const input = screen.getByPlaceholderText("blacklistedCompanies.placeholder");
    await user.type(input, "Example Corp{Enter}");
    const existing = screen.getByText("Outlier").closest("span")!;
    await user.click(within(existing).getByRole("button"));

    await waitFor(() => {
      const payload = mockSaveProfile.mock.calls.at(-1)![0];
      expect(payload.blacklisted_companies).toEqual(["Alignerr", "Example Corp"]);
    });
  });

  it("changing country updates deterministic salary currency", async () => {
    const user = userEvent.setup();
    render(<Profile />);
    await screen.findByLabelText("Markdown profile");

    await user.selectOptions(
      screen.getByDisplayValue("🇺🇸 United States"),
      "DE",
    );

    expect(screen.getByDisplayValue("EUR")).toBeInTheDocument();
  });

  it("keeps the editor mounted when saving fails", async () => {
    mockSaveProfile.mockRejectedValueOnce(new Error("network down"));
    const user = userEvent.setup();
    render(<Profile />);
    await screen.findByLabelText("Markdown profile");

    await user.type(screen.getByLabelText("Markdown profile"), " changed");

    expect(await screen.findByText("saveError")).toBeInTheDocument();
    expect(screen.getByLabelText("Markdown profile")).toBeInTheDocument();
  });

  it("renders defaults if initial loading fails", async () => {
    mockGetProfile.mockRejectedValueOnce(new Error("boom"));
    mockGetCountries.mockRejectedValueOnce(new Error("boom"));
    render(<Profile />);

    expect(await screen.findByText("Changes save automatically")).toBeInTheDocument();
    expect(screen.getByText("English")).toBeInTheDocument();
  });
});
