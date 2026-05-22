import { useEffect, useState } from "react";
import { apiGet } from "./api";

type UseApiOptions = {
  pollIntervalMs?: number;
};

export function useApi<T>(path: string, fallback: T, options: UseApiOptions = {}) {
  const [data, setData] = useState<T>(fallback);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  async function refresh(background = false) {
    if (!background) setLoading(true);
    setError(null);
    try {
      setData(await apiGet<T>(path));
      setLastUpdated(new Date());
    } catch (err) {
      setError(err instanceof Error ? err.message : "request failed");
    } finally {
      if (!background) setLoading(false);
    }
  }

  useEffect(() => {
    let active = true;
    async function run(background = false) {
      if (!active) return;
      await refresh(background);
    }
    void refresh();
    if (!options.pollIntervalMs) {
      return () => {
        active = false;
      };
    }
    const interval = window.setInterval(() => void run(true), options.pollIntervalMs);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [path, options.pollIntervalMs]);

  return { data, error, loading, lastUpdated, refresh };
}
