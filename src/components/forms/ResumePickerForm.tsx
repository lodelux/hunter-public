import { useState, useEffect, useRef, useCallback } from "react";
import { FolderOpen, CheckCircle, Loader2, Upload } from "lucide-react";
import { getSettings, saveSettings, uploadResume } from "../../lib/api";

interface ResumePickerFormProps {
  onSaved?: () => void;
}

const isPdf = (name: string, type?: string) =>
  type === "application/pdf" || name.toLowerCase().endsWith(".pdf");

export default function ResumePickerForm({ onSaved }: ResumePickerFormProps) {
  const [resumePath, setResumePath] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(true);
  const [dragging, setDragging] = useState(false);
  const [dropError, setDropError] = useState("");
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const lastSaved = useRef("");
  const pendingPath = useRef<string | null>(null);
  const saveQueue = useRef<Promise<void>>(Promise.resolve());
  const mounted = useRef(true);

  useEffect(() => {
    let active = true;
    getSettings()
      .then((data) => {
        if (!active) return;
        const path = data.resume_path || "";
        setResumePath(path);
        lastSaved.current = path;
      })
      .catch(() => {})
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, []);

  const doSave = useCallback((path: string) => {
    if (path === lastSaved.current || !path.trim()) return;
    saveQueue.current = saveQueue.current
      .catch(() => {})
      .then(async () => {
        if (mounted.current) setSaving(true);
        try {
          await saveSettings({ resume_path: path });
          lastSaved.current = path;
          if (mounted.current) {
            setSaved(true);
            setTimeout(() => {
              if (mounted.current) setSaved(false);
            }, 2000);
          }
          onSaved?.();
        } catch {
          // Keep the local path; the next edit will retry persistence.
        } finally {
          if (mounted.current) setSaving(false);
        }
      });
  }, [onSaved]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (saveTimer.current) clearTimeout(saveTimer.current);
      const pending = pendingPath.current;
      pendingPath.current = null;
      if (pending) void doSave(pending);
    };
  }, [doSave]);

  const handleChange = (path: string) => {
    setResumePath(path);
    setSaved(false);
    pendingPath.current = path;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      pendingPath.current = null;
      doSave(path);
    }, 800);
  };

  const handleFile = async (file: File | undefined) => {
    if (!file) return;
    if (!isPdf(file.name, file.type)) {
      setDropError("Only PDF files are supported. Please choose a .pdf resume.");
      return;
    }
    setDropError("");
    if (saveTimer.current) clearTimeout(saveTimer.current);
    pendingPath.current = null;
    setSaving(true);
    saveQueue.current = saveQueue.current
      .catch(() => {})
      .then(async () => {
        if (mounted.current) setSaving(true);
        try {
          const result = await uploadResume(file);
          lastSaved.current = result.resume_path;
          if (mounted.current) {
            setResumePath(result.resume_path);
            setSaved(true);
          }
          onSaved?.();
        } catch (error) {
          if (mounted.current) {
            setDropError(error instanceof Error ? error.message : "Resume upload failed.");
          }
        } finally {
          if (mounted.current) {
            setSaving(false);
            if (fileInput.current) fileInput.current.value = "";
          }
        }
      });
    await saveQueue.current;
  };

  const handleBrowse = () => fileInput.current?.click();

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!dragging) setDragging(true);
  };

  const handleDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDropError("");
    setDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragging(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragging(false);

    const file = e.dataTransfer?.files?.[0];
    if (!file) {
      setDropError("No file detected. Please drop a PDF resume.");
      return;
    }
    if (!isPdf(file.name, file.type)) {
      setDropError("Only PDF files are supported. Please drop a .pdf resume.");
      return;
    }
    void handleFile(file);
  };

  if (loading) return <div className="flex justify-center py-4"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>;

  return (
    <div className="space-y-2">
      <label className="block text-sm font-medium text-foreground">Resume PDF Path</label>
      <div className="flex gap-2">
        <input
          value={resumePath}
          onChange={(e) => handleChange(e.target.value)}
          placeholder="/path/to/your/resume.pdf"
          className="flex-1 px-3 py-2 border border-border rounded-lg text-sm"
        />
        <input
          ref={fileInput}
          type="file"
          accept="application/pdf,.pdf"
          className="hidden"
          onChange={(event) => void handleFile(event.target.files?.[0])}
        />
        <button type="button" onClick={handleBrowse} className="flex items-center gap-2 px-3 py-2 border border-border rounded-lg text-sm hover:bg-secondary">
          <FolderOpen className="w-4 h-4" /> Upload
        </button>
      </div>
      <div
        role="button"
        tabIndex={0}
        aria-label="Drop a PDF resume here to upload"
        onClick={handleBrowse}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handleBrowse(); } }}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        className={`flex flex-col items-center justify-center gap-1 px-3 py-4 border-2 border-dashed rounded-lg text-sm text-center cursor-pointer transition-colors ${
          dragging
            ? "border-primary bg-secondary text-foreground"
            : "border-border text-muted-foreground hover:bg-secondary"
        }`}
      >
        <Upload className="w-5 h-5" />
        <span>{dragging ? "Drop your PDF resume" : "Drag & drop a PDF resume here, or click to browse"}</span>
      </div>
      <div className="flex items-center gap-2 min-h-5">
        {dropError && <span className="text-xs text-red-600">{dropError}</span>}
        {!dropError && saving && <span className="flex items-center gap-1 text-xs text-muted-foreground"><Loader2 className="w-3 h-3 animate-spin" /> Saving...</span>}
        {!dropError && saved && <span className="flex items-center gap-1 text-xs text-green-600"><CheckCircle className="w-3 h-3" /> Saved ✓</span>}
        {!dropError && !saving && !saved && resumePath && <span className="text-xs text-muted-foreground">PDF file used for job applications</span>}
      </div>
    </div>
  );
}
