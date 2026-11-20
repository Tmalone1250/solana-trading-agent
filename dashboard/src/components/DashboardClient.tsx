"use client";

import { useEffect, useState } from "react";
import { ActivePositionHero } from "@/components/ActivePositionHero";
import { FocusStateHeader } from "@/components/FocusStateHeader";
import { RecentTradesTable } from "@/components/RecentTradesTable";
import { WatchlistTable } from "@/components/WatchlistTable";
import { TelemetryPayload } from "@/lib/types";

const AGENT_URL = (
  process.env.NEXT_PUBLIC_SOLANA_AGENT_URL || "http://127.0.0.1:8002"
).replace(/\/+$/, "");

const POLL_INTERVAL_MS = 3000;

export function DashboardClient() {
  const [telemetry, setTelemetry] = useState<TelemetryPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastFetchedAt, setLastFetchedAt] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    async function poll() {
      try {
        const res = await fetch(`${AGENT_URL}/api/telemetry`, {
          method: "GET",
          cache: "no-store",
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const data: TelemetryPayload = await res.json();
        if (!active) return;
        setTelemetry(data);
        setError(null);
        setLastFetchedAt(
          new Date().toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          })
        );
      } catch (err) {
        if (!active) return;
        setError(err instanceof Error ? err.message : "Network error");
      }
    }

    poll();
    const timer = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  return (
    <main className="mx-auto flex min-h-screen max-w-7xl flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <FocusStateHeader
        telemetry={telemetry}
        agentUrl={AGENT_URL}
        error={error}
        lastFetchedAt={lastFetchedAt}
      />

      <ActivePositionHero telemetry={telemetry} />

      <WatchlistTable slots={telemetry?.active_watchlist ?? []} />

      <RecentTradesTable trades={telemetry?.recent_trades ?? []} />
    </main>
  );
}
