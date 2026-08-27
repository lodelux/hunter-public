import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import LLMSettingsForm from "./LLMSettingsForm";
import {
  getLLMSettings,
  saveLLMSettings,
  testLLMConnection,
} from "../../lib/api";

// i18n: identity translator so assertions can use raw keys.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock("../../lib/api", () => ({
  getLLMSettings: vi.fn(),
  saveLLMSettings: vi.fn(),
  testLLMConnection: vi.fn(),
}));

const mockGetLLMSettings = vi.mocked(getLLMSettings);
const mockSaveLLMSettings = vi.mocked(saveLLMSettings);
const mockTestLLMConnection = vi.mocked(testLLMConnection);

const baseSettings = {
  provider: "openrouter",
  openai: { api_key: "", model: "gpt-5.6-sol" },
  anthropic: { api_key: "", model: "claude-sonnet-4-5" },
  gemini: { api_key: "", model: "gemini-2.5-pro" },
  bedrock: {
    access_key: "",
    secret_key: "",
    region: "us-west-2",
    model: "us.anthropic.claude-sonnet-4-6",
    auth_mode: "profile",
    profile_name: "default",
  },
  ollama: { base_url: "http://localhost:11434", model: "" },
  openrouter: { api_key: "", model: "qwen/qwen3.6-plus" },
  openai_compatible: { base_url: "", api_key: "", model: "" },
};

describe("LLMSettingsForm", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    mockGetLLMSettings.mockResolvedValue(structuredClone(baseSettings) as never);
    mockSaveLLMSettings.mockResolvedValue({ success: true } as never);
    mockTestLLMConnection.mockResolvedValue({ success: true, message: "ok" } as never);
  });

  it("renders a loading spinner before settings load", async () => {
    render(<LLMSettingsForm />);
    // The provider radios are not present while loading.
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    await waitFor(() => expect(mockGetLLMSettings).toHaveBeenCalled());
    await screen.findByText("selectProvider");
  });

  it("renders all provider radios and starts with openrouter selected", async () => {
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(7);
    const openrouter = screen.getByRole("radio", { name: /providers\.openrouter/ });
    expect(openrouter).toBeChecked();
    // OpenRouter config field (api key) is present.
    expect(screen.getByPlaceholderText("sk-or-v1-...")).toBeInTheDocument();
  });

  it("switches provider via radio, renders that provider's config, and immediate-saves", async () => {
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    await user.click(screen.getByRole("radio", { name: /providers\.openai\b/ }));

    // OpenAI key placeholder appears.
    expect(await screen.findByPlaceholderText("sk-...")).toBeInTheDocument();
    expect(screen.getByDisplayValue("gpt-5.6-sol")).toBeInTheDocument();
    await waitFor(() =>
      expect(mockSaveLLMSettings).toHaveBeenCalledWith(
        expect.objectContaining({ provider: "openai" }),
      ),
    );
  });

  it("debounce-saves api key edits (autosave path)", async () => {
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    const keyInput = screen.getByPlaceholderText("sk-or-v1-...");
    await user.type(keyInput, "sk-or-v1-abc");

    await waitFor(
      () =>
        expect(mockSaveLLMSettings).toHaveBeenCalledWith(
          expect.objectContaining({
            openrouter: expect.objectContaining({ api_key: "sk-or-v1-abc" }),
          }),
        ),
      { timeout: 2000 },
    );
  });

  it("flushes a pending edit when the form unmounts", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    await user.type(screen.getByPlaceholderText("sk-or-v1-..."), "pending-key");
    unmount();

    await waitFor(() =>
      expect(mockSaveLLMSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          openrouter: expect.objectContaining({ api_key: "pending-key" }),
        }),
      ),
    );
  });

  it("cancels a pending edit timer when the provider is saved immediately", async () => {
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    await user.type(screen.getByPlaceholderText("sk-or-v1-..."), "latest-key");
    await user.click(screen.getByRole("radio", { name: /providers\.openai\b/ }));

    await new Promise((resolve) => setTimeout(resolve, 1000));
    expect(mockSaveLLMSettings).toHaveBeenCalledTimes(1);
    expect(mockSaveLLMSettings).toHaveBeenLastCalledWith(
      expect.objectContaining({
        provider: "openai",
        openrouter: expect.objectContaining({ api_key: "latest-key" }),
      }),
    );
  });

  it("Test Connection success shows the returned message", async () => {
    mockTestLLMConnection.mockResolvedValue({
      success: true,
      message: "Connected!",
    } as never);
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    await user.click(screen.getByRole("button", { name: /status\.testConnection/ }));

    expect(await screen.findByText("Connected!")).toBeInTheDocument();
    expect(mockTestLLMConnection).toHaveBeenCalled();
  });

  it("Test Connection failure shows an error message", async () => {
    mockTestLLMConnection.mockResolvedValue({
      success: false,
      message: "Bad key",
    } as never);
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    await user.click(screen.getByRole("button", { name: /status\.testConnection/ }));

    expect(await screen.findByText("Bad key")).toBeInTheDocument();
  });

  it("Test Connection shows the backend's friendly invalid-key message (#48)", async () => {
    mockTestLLMConnection.mockResolvedValue({
      success: false,
      message: "Invalid API key. Please check your key and try again.",
    } as never);
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    await user.click(screen.getByRole("button", { name: /status\.testConnection/ }));
    expect(
      await screen.findByText("Invalid API key. Please check your key and try again."),
    ).toBeInTheDocument();
  });

  it("Test Connection rejection surfaces the thrown error message", async () => {
    mockTestLLMConnection.mockRejectedValue(new Error("timeout"));
    const user = userEvent.setup();
    render(<LLMSettingsForm />);
    await screen.findByText("selectProvider");

    await user.click(screen.getByRole("button", { name: /status\.testConnection/ }));
    expect(await screen.findByText("timeout")).toBeInTheDocument();
  });

  describe("model text input", () => {
    it("lets the user type any OpenRouter model and saves it", async () => {
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      const modelInput = screen.getByDisplayValue("qwen/qwen3.6-plus");
      expect(modelInput).toHaveAttribute("type", "text");
      await user.clear(modelInput);
      await user.type(modelInput, "openai/gpt-oss-120b:free");

      await waitFor(
        () =>
          expect(mockSaveLLMSettings).toHaveBeenCalledWith(
            expect.objectContaining({
              openrouter: expect.objectContaining({
                model: "openai/gpt-oss-120b:free",
              }),
            }),
          ),
        { timeout: 2000 },
      );
    });

    it("shows a saved model without requiring it to be in a known list", async () => {
      const custom = structuredClone(baseSettings);
      custom.openrouter.model = "some/unknown-model:free";
      mockGetLLMSettings.mockResolvedValue(custom as never);

      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      expect(screen.getByDisplayValue("some/unknown-model:free")).toBeInTheDocument();
    });
  });

  describe("provider config fields", () => {
    it("bedrock: toggling auth mode to keys reveals access/secret key inputs", async () => {
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      await user.click(screen.getByRole("radio", { name: /providers\.bedrock/ }));
      // Profile mode renders first (profile name input present).
      await screen.findByText("labels.profileName");

      // Switch to access/secret key auth.
      await user.click(screen.getByRole("button", { name: "labels.accessKeySecretKey" }));
      expect(await screen.findByPlaceholderText("AKIA...")).toBeInTheDocument();
    });

    it("ollama: shows a free-text model input", async () => {
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      await user.click(screen.getByRole("radio", { name: /providers\.ollama/ }));

      expect(await screen.findByPlaceholderText("llama3.1")).toHaveAttribute("type", "text");
    });

    it("gemini: renders API key + free-text model fields", async () => {
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      await user.click(screen.getByRole("radio", { name: /providers\.gemini/ }));

      // API key password field appears.
      expect(await screen.findByPlaceholderText("AIza...")).toBeInTheDocument();
      expect(screen.getByDisplayValue("gemini-2.5-pro")).toHaveAttribute("type", "text");
      // Switching provider immediate-saves with provider=gemini.
      await waitFor(() =>
        expect(mockSaveLLMSettings).toHaveBeenCalledWith(
          expect.objectContaining({ provider: "gemini" }),
        ),
      );
    });

    it("gemini: editing the API key debounce-saves", async () => {
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      await user.click(screen.getByRole("radio", { name: /providers\.gemini/ }));
      const keyInput = await screen.findByPlaceholderText("AIza...");
      await user.type(keyInput, "AIzaABC");

      await waitFor(
        () =>
          expect(mockSaveLLMSettings).toHaveBeenCalledWith(
            expect.objectContaining({
              gemini: expect.objectContaining({ api_key: "AIzaABC" }),
            }),
          ),
        { timeout: 2000 },
      );
    });

    it("openai_compatible: renders base URL and model inputs", async () => {
      const user = userEvent.setup();
      render(<LLMSettingsForm />);
      await screen.findByText("selectProvider");

      await user.click(
        screen.getByRole("radio", { name: /providers\.openai_compatible/ }),
      );
      expect(
        await screen.findByPlaceholderText("https://api.together.xyz/v1"),
      ).toBeInTheDocument();
      expect(
        screen.getByPlaceholderText("meta-llama/Llama-3-70b-chat-hf"),
      ).toBeInTheDocument();
    });
  });
});
