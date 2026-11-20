"use client";

import { Activity, ShieldAlert, ShieldCheck, Wallet } from "lucide-react";
import { FocusState, TelemetryPayload, formatShortTime, formatSol } from "@/lib/types";

interface Props {
  telemetry: TelemetryPayload | null;
  agentUrl: string;
  error: string | null;
  lastFetchedAt: string | null;
}

const STATE_STYLES: Record<FocusState, { badge: string; dot: string; label: string }> = {
  SCANNING: {
    badge: "bg-blue-500/15 text-blue-300 border-blue-500/40",
    dot: "bg-blue-400",
    label: "SCANNING",
  },
  IN_POSITION: {
    badge: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
    dot: "bg-emerald-400",
    label: "IN_POSITION",
  },
  WATCHING_REENTRY: {
    badge: "bg-amber-500/15 text-amber-300 border-amber-500/40",
    dot: "bg-amber-400",
    label: "WATCHING_REENTRY",
  },
};

export function FocusStateHeader({ telemetry, agentUrl, error, lastFetchedAt }: Props) {
  const stateKey: FocusState = telemetry?.focus_state ?? "SCANNING";
  const st = STATE_STYLES[stateKey] ?? STATE_STYLES.SCANNING;
  const isLive = telemetry?.execution_mode === "live";
  const dailyPnl = telemetry?.daily_pnl_sol ?? 0;
  const halted = telemetry?.daily_max_loss_hit ?? false;

  return (
    <header className="rounded-2xl border border-slate-800/80 bg-slate-900/70 p-4 backdrop-blur sm:p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        {/* Left: Title + Live Focus State Badge + Mode */}
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl border border-slate-700/80 bg-slate-950 text-sky-400">
              <Activity className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-base font-semibold tracking-tight text-slate-100 sm:text-lg">
                solana-meme-agent
              </h1>
              <p className="font-mono text-[11px] text-slate-400">
                {agentUrl} {lastFetchedAt ? `• polled ${lastFetchedAt}` : ""}
              </p>
            </div>
          </div>

          <span
            className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 font-mono text-xs font-semibold tracking-wide ${st.badge}`}
          >
            <span className={`h-2 w-2 animate-pulse rounded-full ${st.dot}`} />
            {st.label}
          </span>

          <span
            className={`inline-flex items-center rounded-full border px-2.5 py-1 font-mono text-[11px] font-semibold uppercase tracking-wider ${
              isLive
                ? "border-rose-500/50 bg-rose-500/15 text-rose-300"
                : "border-slate-700 bg-slate-800/80 text-slate-300"
            }`}
          >
            {telemetry ? telemetry.execution_mode.toUpperCase() : "CONNECTING"}
          </span>

          {telemetry?.hunting_lane && (
            <span className="rounded-full border border-slate-800 bg-slate-950/70 px-2.5 py-1 font-mono text-[11px] text-slate-400">
              lane: {telemetry.hunting_lane}
            </span>
          )}
        </div>

        {/* Right: Equity, Daily PnL, Circuit Breaker */}
        <div className="grid grid-cols-3 gap-2.5 sm:flex sm:items-center sm:gap-3">
          <div className="rounded-xl border border-slate-800 bg-slate-950/70 px-3 py-2">
            <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-slate-400">
              <Wallet className="h-3 w-3 text-sky-400" />
              SOL Equity
            </div>
            <div className="mt-0.5 font-mono text-sm font-semibold tabular-nums text-slate-100">
              {formatSol(telemetry?.sol_balance)}
            </div>
          </div>

          <div className="rounded-xl border border-slate-800 bg-slate-950/70 px-3 py-2">
            <div className="text-[10px] font-medium uppercase tracking-wider text-slate-400">
              Daily Realized
            </div>
            <div
              className={`mt-0.5 font-mono text-sm font-semibold tabular-nums ${
                dailyPnl > 0
                  ? "text-emerald-400"
                  : dailyPnl < 0
                    ? "text-rose-400"
                    : "text-slate-300"
              }`}
            >
              {formatSol(dailyPnl, true)}
            </div>
          </div>

          <div
            className={`rounded-xl border px-3 py-2 ${
              halted
                ? "border-rose-500/50 bg-rose-500/10"
                : "border-slate-800 bg-slate-950/70"
            }`}
          >
            <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-slate-400">
              {halted ? (
                <ShieldAlert className="h-3 w-3 text-rose-400" />
              ) : (
                <ShieldCheck className="h-3 w-3 text-emerald-400" />
              )}
              Circuit Breaker
            </div>
            <div
              className={`mt-0.5 font-mono text-xs font-semibold ${
                halted ? "text-rose-300" : "text-emerald-400"
              }`}
            >
              {halted ? "HALTED (-20%)" : "ARMED OK"}
            </div>
          </div>
        </div>
      </div>

      {error && (
        <div className="mt-3 rounded-xl border border-rose-500/40 bg-rose-500/10 px-3 py-2 font-mono text-xs text-rose-300">
          Telemetry offline ({error}) — retrying every 3s...
        </div>
      )}

      {telemetry?.latest_decision_1_rationale && (
        <div className="mt-3 flex items-center justify-between gap-3 rounded-xl border border-slate-800/80 bg-slate-950/70 px-3 py-2 font-mono text-xs text-slate-300">
          <span className="truncate">{telemetry.latest_decision_1_rationale}</span>
          <span className="shrink-0 text-[11px] text-slate-500">
            {formatShortTime(telemetry.updated_at)}
          </span>
        </div>
      )}
    </header>
  );
}
