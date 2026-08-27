import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ResumePickerForm from "./ResumePickerForm";
import { getSettings, saveSettings, uploadResume } from "../../lib/api";

vi.mock("../../lib/api", () => ({
  getSettings: vi.fn(),
  saveSettings: vi.fn(),
  uploadResume: vi.fn(),
}));

const mockGetSettings = vi.mocked(getSettings);
const mockSaveSettings = vi.mocked(saveSettings);
const mockUploadResume = vi.mocked(uploadResume);

const getFileInput = (container: HTMLElement) => {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]');
  expect(input).not.toBeNull();
  return input!;
};

describe("ResumePickerForm", () => {
  beforeEach(() => {
    mockGetSettings.mockResolvedValue({ resume_path: "" } as never);
    mockSaveSettings.mockResolvedValue({ success: true } as never);
    mockUploadResume.mockResolvedValue({
      success: true,
      resume_path: "/data/resumes/resume.pdf",
    });
  });

  it("shows a loading spinner before settings resolve, then the input", async () => {
    render(<ResumePickerForm />);
    // While loading there is no labelled input yet.
    expect(screen.queryByText("Resume PDF Path")).not.toBeInTheDocument();

    expect(await screen.findByText("Resume PDF Path")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("/path/to/your/resume.pdf")).toBeInTheDocument();
  });

  it("pre-populates the input from existing settings", async () => {
    mockGetSettings.mockResolvedValue({ resume_path: "/home/me/cv.pdf" } as never);
    render(<ResumePickerForm />);

    const input = await screen.findByPlaceholderText("/path/to/your/resume.pdf");
    expect(input).toHaveValue("/home/me/cv.pdf");
  });

  it("debounce-saves a typed path and calls onSaved", async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    render(<ResumePickerForm onSaved={onSaved} />);

    const input = await screen.findByPlaceholderText("/path/to/your/resume.pdf");
    await user.type(input, "/docs/resume.pdf");

    await waitFor(
      () => {
        expect(mockSaveSettings).toHaveBeenCalledWith(
          expect.objectContaining({ resume_path: "/docs/resume.pdf" }),
        );
      },
      { timeout: 2000 },
    );
    expect(onSaved).toHaveBeenCalled();
    expect(await screen.findByText(/Saved/)).toBeInTheDocument();
  });

  it("does not save when the path matches the already-saved value", async () => {
    mockGetSettings.mockResolvedValue({ resume_path: "/same.pdf" } as never);
    const user = userEvent.setup();
    render(<ResumePickerForm />);

    const input = await screen.findByPlaceholderText("/path/to/your/resume.pdf");
    // Re-typing the identical existing value should be a no-op save.
    await user.clear(input);
    await user.type(input, "/same.pdf");

    // Wait past the debounce window.
    await new Promise((r) => setTimeout(r, 1000));
    expect(mockSaveSettings).not.toHaveBeenCalled();
  });

  it("uploads a chosen PDF and uses the returned host path", async () => {
    const user = userEvent.setup();
    const { container } = render(<ResumePickerForm />);

    await screen.findByText("Resume PDF Path");
    const file = new File(["%PDF-1.4"], "resume.pdf", { type: "application/pdf" });
    await user.upload(getFileInput(container), file);

    const input = await screen.findByPlaceholderText("/path/to/your/resume.pdf");
    await waitFor(() => expect(input).toHaveValue("/data/resumes/resume.pdf"));
    expect(mockUploadResume).toHaveBeenCalledWith(file);
    expect(mockSaveSettings).not.toHaveBeenCalled();
  });

  it("does not let an older typed path overwrite an uploaded path", async () => {
    const user = userEvent.setup();
    const { container } = render(<ResumePickerForm />);

    const input = await screen.findByPlaceholderText("/path/to/your/resume.pdf");
    await user.type(input, "/typed/old.pdf");
    const file = new File(["%PDF-1.4"], "latest.pdf", { type: "application/pdf" });
    await user.upload(getFileInput(container), file);
    await new Promise((resolve) => setTimeout(resolve, 1000));

    expect(mockUploadResume).toHaveBeenCalledWith(file);
    expect(mockSaveSettings).not.toHaveBeenCalled();
    expect(input).toHaveValue("/data/resumes/resume.pdf");
  });

  it("waits for an in-flight typed-path save before uploading", async () => {
    let releaseSave = () => {};
    mockSaveSettings.mockImplementationOnce(
      () => new Promise((resolve) => {
        releaseSave = () => resolve({ success: true } as never);
      }),
    );
    const user = userEvent.setup();
    const { container } = render(<ResumePickerForm />);
    const input = await screen.findByPlaceholderText("/path/to/your/resume.pdf");

    await user.type(input, "/typed/old.pdf");
    await waitFor(() => expect(mockSaveSettings).toHaveBeenCalled(), { timeout: 2000 });
    const file = new File(["%PDF-1.4"], "latest.pdf", { type: "application/pdf" });
    await user.upload(getFileInput(container), file);
    expect(mockUploadResume).not.toHaveBeenCalled();

    act(() => releaseSave());
    await waitFor(() => expect(mockUploadResume).toHaveBeenCalledWith(file));
    await waitFor(() => expect(input).toHaveValue("/data/resumes/resume.pdf"));
  });

  it("flushes a pending typed path when the form unmounts", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<ResumePickerForm />);
    const input = await screen.findByPlaceholderText("/path/to/your/resume.pdf");

    await user.type(input, "/pending/resume.pdf");
    unmount();

    await waitFor(() =>
      expect(mockSaveSettings).toHaveBeenCalledWith({
        resume_path: "/pending/resume.pdf",
      }),
    );
  });

  it("does nothing when Upload is clicked without choosing a file", async () => {
    const user = userEvent.setup();
    render(<ResumePickerForm />);

    await screen.findByText("Resume PDF Path");
    await user.click(screen.getByRole("button", { name: /^Upload$/i }));

    await new Promise((r) => setTimeout(r, 100));
    expect(mockUploadResume).not.toHaveBeenCalled();
    expect(mockSaveSettings).not.toHaveBeenCalled();
    const input = screen.getByPlaceholderText("/path/to/your/resume.pdf");
    expect(input).toHaveValue("");
  });

  it("survives a getSettings failure on load (no crash, empty input)", async () => {
    mockGetSettings.mockRejectedValue(new Error("boom"));
    render(<ResumePickerForm />);

    const input = await screen.findByPlaceholderText("/path/to/your/resume.pdf");
    expect(input).toHaveValue("");
  });

  // ── Drag-and-drop ─────────────────────────────────────────────────────────
  const getDropZone = () =>
    screen.getByRole("button", { name: /Drop a PDF resume here/i });

  it("highlights the drop zone on drag-over", async () => {
    render(<ResumePickerForm />);
    await screen.findByText("Resume PDF Path");

    const zone = getDropZone();
    expect(zone.className).not.toContain("border-primary");
    expect(
      screen.getByText(/Drag & drop a PDF resume here/i),
    ).toBeInTheDocument();

    fireEvent.dragEnter(zone, { dataTransfer: { types: ["Files"] } });
    fireEvent.dragOver(zone, { dataTransfer: { types: ["Files"] } });

    expect(zone.className).toContain("border-primary");
    expect(screen.getByText(/Drop your PDF resume/i)).toBeInTheDocument();
  });

  it("removes the highlight on drag-leave", async () => {
    render(<ResumePickerForm />);
    await screen.findByText("Resume PDF Path");

    const zone = getDropZone();
    fireEvent.dragEnter(zone, { dataTransfer: { types: ["Files"] } });
    expect(zone.className).toContain("border-primary");

    fireEvent.dragLeave(zone, { dataTransfer: { types: ["Files"] } });
    expect(zone.className).not.toContain("border-primary");
  });

  it("saves and shows success when a PDF file is dropped", async () => {
    const onSaved = vi.fn();
    render(<ResumePickerForm onSaved={onSaved} />);
    await screen.findByText("Resume PDF Path");

    const zone = getDropZone();
    const file = new File(["%PDF-1.4"], "resume.pdf", {
      type: "application/pdf",
    });

    fireEvent.drop(zone, { dataTransfer: { files: [file], types: ["Files"] } });

    await waitFor(() => expect(mockUploadResume).toHaveBeenCalledWith(file));
    expect(onSaved).toHaveBeenCalled();
    expect(await screen.findByText(/Saved/)).toBeInTheDocument();
    const input = screen.getByPlaceholderText("/path/to/your/resume.pdf");
    expect(input).toHaveValue("/data/resumes/resume.pdf");
  });

  it("rejects a non-PDF drop with an error and does not save", async () => {
    render(<ResumePickerForm />);
    await screen.findByText("Resume PDF Path");

    const zone = getDropZone();
    const file = new File(["hello"], "notes.txt", { type: "text/plain" });

    fireEvent.drop(zone, { dataTransfer: { files: [file], types: ["Files"] } });

    expect(await screen.findByText(/Only PDF files are supported/i)).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 200));
    expect(mockSaveSettings).not.toHaveBeenCalled();
    const input = screen.getByPlaceholderText("/path/to/your/resume.pdf");
    expect(input).toHaveValue("");
  });

  it("accepts a .pdf file even when the MIME type is missing", async () => {
    render(<ResumePickerForm />);
    await screen.findByText("Resume PDF Path");

    const zone = getDropZone();
    const file = new File(["%PDF-1.4"], "MyResume.PDF", { type: "" });

    fireEvent.drop(zone, { dataTransfer: { files: [file], types: ["Files"] } });

    await waitFor(() => expect(mockUploadResume).toHaveBeenCalledWith(file));
  });
});
