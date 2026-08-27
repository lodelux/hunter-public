import { useCallback, useEffect, useRef, useState } from "react";

export type AutosaveStatus = "idle" | "pending" | "saving" | "saved" | "error";

export function useAutosave<T>(
  value: T,
  save: (value: T) => Promise<unknown>,
  enabled: boolean,
  delay = 800,
) {
  const [status, setStatus] = useState<AutosaveStatus>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pending = useRef<{ value: T; key: string } | null>(null);
  const latestValue = useRef(value);
  const latestKey = useRef("");
  const serverKey = useRef("");
  const queuedSaves = useRef(0);
  const initialized = useRef(false);
  const mounted = useRef(true);
  const queue = useRef<Promise<void>>(Promise.resolve());
  const key = JSON.stringify(value);

  const persist = useCallback((snapshot: T, snapshotKey: string) => {
    queuedSaves.current += 1;
    queue.current = queue.current
      .catch(() => {})
      .then(async () => {
        if (mounted.current && latestKey.current === snapshotKey) {
          setStatus("saving");
        }
        try {
          await save(snapshot);
          serverKey.current = snapshotKey;
          if (mounted.current && latestKey.current === snapshotKey) {
            setStatus("saved");
          }
        } catch (error) {
          if (mounted.current && latestKey.current === snapshotKey) {
            setStatus("error");
          }
          console.error("Autosave failed:", error);
        } finally {
          queuedSaves.current -= 1;
        }
      });
    return queue.current;
  }, [save]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (timer.current) clearTimeout(timer.current);
      const snapshot = pending.current;
      pending.current = null;
      if (snapshot) void persist(snapshot.value, snapshot.key);
    };
  }, [persist]);

  useEffect(() => {
    latestValue.current = value;
    latestKey.current = key;
    if (!enabled) return;

    if (!initialized.current) {
      initialized.current = true;
      serverKey.current = key;
      return;
    }
    if (key === serverKey.current && queuedSaves.current === 0) {
      pending.current = null;
      queueMicrotask(() => {
        if (mounted.current && latestKey.current === key) setStatus("idle");
      });
      return;
    }

    queueMicrotask(() => {
      if (mounted.current && latestKey.current === key) setStatus("pending");
    });
    pending.current = { value, key };
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      timer.current = null;
      pending.current = null;
      void persist(value, key);
    }, delay);

    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [delay, enabled, key, persist, value]);

  const clearError = useCallback(() => {
    setStatus((current) => current === "error" ? "idle" : current);
  }, []);

  const retry = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    pending.current = null;
    void persist(latestValue.current, latestKey.current);
  }, [persist]);

  return { status, clearError, retry };
}
