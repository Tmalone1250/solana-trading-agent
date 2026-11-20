"use client";

import { ExternalLink, Radar, TrendingUp } from "lucide-react";
import { TelemetryPayload, formatSol } from "@/lib/types";

interface Props {
  telemetry: TelemetryPayload | null;
}

function clampPct(value: number, min: number, max: number): number {
  return Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100));
}

export function ActivePositionHero({ telemetry }: Props) {
  const focus = telemetry?.focus_state ?? "SCANNING";
  const pnl = telemetry?.live_jupiter_pnl;
  const mint = telemetry?.active_mint;
  const symbol = telemetry?.active_symbol;

  if (!pnl || !mint || focus === "SCANNING") {
    return (
      <section className="rounded-2xl border border-slate-800/80 bg-slate-900/50 p-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-blue-500/30 bg-blue-500/10 text-blue-400">
              <Radar className="h-5 w-5" />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-slate-200">
                No Active Focus Position
              </h2>
              <p className="text-xs text-slate-400">
                3-State Focus Machine is in <span className="font-mono text-blue-300">SCANNING</span>{" "}
                — evaluating 5 rotating slots through the Scalper Expert Entry Engine &amp; Decision 1.
              </p>
            </div>
          </div>
          <div className="hidden font-mono text-xs text-slate-500 sm:block">
            Cooldowns active: {telemetry?.active_cooldowns?.length ?? 0}
          </div>
        </div>
      </section>
    );
  }

  const isWatching = focus === "WATCHING_REENTRY";
  const pnlPct = pnl.pnl_pct;
  const peakPct =
    pnl.entry_sol > 0 ? ((pnl.peak_sol_value - pnl.entry_sol) / pnl.entry_sol) * 100 : 0;

  // Risk gauge scale: -15% to max(+25%, ceil(peakPct + 5))
  const gaugeMin = -15;
  const gaugeMax = Math.max(25, Math.ceil((Math.max(peakPct, pnlPct) + 5) / 5) * 5);

  const hardStopPos = clampPct(-10, gaugeMin, gaugeMax);
  const zeroPos = clampPct(0, gaugeMin, gaugeMax);
  const beFloorPos = clampPct(2, gaugeMin, gaugeMax);
  const beArmPos = clampPct(8, gaugeMin, gaugeMax);
  const trailArmPos = clampPct(12, gaugeMin, gaugeMax);
  const currentPos = clampPct(pnlPct, gaugeMin, gaugeMax);
  const peakPos = clampPct(peakPct, gaugeMin, gaugeMax);

  // Live trailing TP trigger level (-6% pullback from peak) when armed
  const trailStopPct = pnl.trail_armed
    ? ((pnl.peak_sol_value * 0.94 - pnl.entry_sol) / Math.max(pnl.entry_sol, 1e-9)) * 100
    : null;
  const trailStopPos =
    trailStopPct !== null ? clampPct(trailStopPct, gaugeMin, gaugeMax) : null;

  const dexUrl = `https://dexscreener.com/solana/${mint}`;

  return (
    <section
      className={`rounded-2xl border p-5 ${
        isWatching
          ? "border-amber-500/40 bg-amber-950/15"
          : "border-emerald-500/40 bg-emerald-950/15"
      }`}
    >
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex items-center gap-2.5">
            <TrendingUp
              className={`h-5 w-5 ${isWatching ? "text-amber-400" : "text-emerald-400"}`}
            />
            <span className="font-mono text-xs uppercase tracking-wider text-slate-400">
              {isWatching ? "Watching Leg 2 Re-Entry" : "Active Focused Position"}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-3">
            <a
              href={dexUrl}
              target="_blank"
              rel="noreferrer"
              className="group inline-flex items-center gap-1.5 text-2xl font-bold tracking-tight text-slate-100 hover:text-sky-300"
            >
              ${symbol}
              <ExternalLink className="h-4 w-4 text-slate-500 transition group-hover:text-sky-400" />
            </a>
            <span className="rounded-md border border-slate-800 bg-slate-950/80 px-2 py-0.5 font-mono text-xs text-slate-400">
              {mint.slice(0, 6)}...{mint.slice(-6)}
            </span>
          </div>
        </div>

        {/* Live Jupiter Quote vs Entry */}
        <div className="grid grid-cols-3 gap-3 sm:gap-4">
          <div className="rounded-xl border border-slate-800/80 bg-slate-950/70 px-3.5 py-2.5">
            <div className="text-[10px] font-medium uppercase tracking-wider text-slate-400">
              Entry SOL
            </div>
            <div className="mt-0.5 font-mono text-sm font-semibold tabular-nums text-slate-200">
              {formatSol(pnl.entry_sol)}
            </div>
          </div>

          <div className="rounded-xl border border-slate-800/80 bg-slate-950/70 px-3.5 py-2.5">
            <div className="text-[10px] font-medium uppercase tracking-wider text-slate-400">
              Jupiter Exit
            </div>
            <div className="mt-0.5 font-mono text-sm font-semibold tabular-nums text-slate-100">
              {formatSol(pnl.current_sol_value)}
            </div>
          </div>

          <div className="rounded-xl border border-slate-800/80 bg-slate-950/70 px-3.5 py-2.5">
            <div className="text-[10px] font-medium uppercase tracking-wider text-slate-400">
              Unrealized PnL
            </div>
            <div
              className={`mt-0.5 font-mono text-sm font-bold tabular-nums ${
                pnlPct > 0
                  ? "text-emerald-400"
                  : pnlPct < 0
                    ? "text-rose-400"
                    : "text-slate-300"
              }`}
            >
              {pnlPct > 0 ? "+" : ""}
              {pnlPct.toFixed(2)}%{" "}
              <span className="text-xs font-normal text-slate-400">
                ({formatSol(pnl.pnl_sol, true)})
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Horizontal Risk Gauge */}
      <div className="mt-5 rounded-xl border border-slate-800/80 bg-slate-950/70 p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-[11px] font-mono">
          <span className="text-slate-400">
            RISK GAUGE ({gaugeMin}% to +{gaugeMax}%)
          </span>
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-rose-400">Hard Stop: -10%</span>
            <span className={pnl.breakeven_armed ? "font-semibold text-cyan-300" : "text-slate-400"}>
              BE Shield: +8% / +2% {pnl.breakeven_armed ? "(ARMED)" : ""}
            </span>
            <span className={pnl.trail_armed ? "font-semibold text-amber-300" : "text-slate-400"}>
              Trailing TP: +12% / -6%{" "}
              {pnl.trail_armed && trailStopPct !== null
                ? `(ARMED @ ${trailStopPct >= 0 ? "+" : ""}${trailStopPct.toFixed(1)}%)`
                : ""}
            </span>
          </div>
        </div>

        <div className="relative h-4 w-full overflow-hidden rounded-full bg-slate-900">
          {/* Danger zone left of -10% */}
          <div
            className="absolute inset-y-0 left-0 bg-rose-500/25"
            style={{ width: `${hardStopPos}%` }}
          />
          {/* Breakeven +2% to +12% zone */}
          <div
            className="absolute inset-y-0 bg-cyan-500/15"
            style={{ left: `${beFloorPos}%`, width: `${Math.max(0, trailArmPos - beFloorPos)}%` }}
          />
          {/* Trailing TP zone >= +12% */}
          <div
            className="absolute inset-y-0 right-0 bg-emerald-500/15"
            style={{ left: `${trailArmPos}%` }}
          />

          {/* Threshold ticks */}
          <div
            className="absolute inset-y-0 w-0.5 bg-rose-500"
            style={{ left: `${hardStopPos}%` }}
            title="Hard Stop (-10%)"
          />
          <div
            className="absolute inset-y-0 w-px bg-slate-600"
            style={{ left: `${zeroPos}%` }}
            title="Entry (0%)"
          />
          <div
            className="absolute inset-y-0 w-0.5 bg-cyan-400"
            style={{ left: `${beFloorPos}%` }}
            title="Breakeven Floor (+2%)"
          />
          <div
            className="absolute inset-y-0 w-0.5 bg-teal-400/70"
            style={{ left: `${beArmPos}%` }}
            title="Breakeven Arm (+8%)"
          />
          <div
            className="absolute inset-y-0 w-0.5 bg-amber-400"
            style={{ left: `${trailArmPos}%` }}
            title="Trailing TP Arm (+12%)"
          />

          {/* Dynamic Trailing Stop line when armed */}
          {trailStopPos !== null && (
            <div
              className="absolute inset-y-0 w-1 bg-amber-300"
              style={{ left: `${trailStopPos}%` }}
              title="Active Trailing Stop (-6% from peak)"
            />
          )}

          {/* Peak marker */}
          <div
            className="absolute top-0.5 h-3 w-1 rounded-full bg-slate-400/70"
            style={{ left: `calc(${peakPos}% - 2px)` }}
            title={`Peak: +${peakPct.toFixed(2)}%`}
          />

          {/* Current live PnL pointer */}
          <div
            className={`absolute top-0 h-4 w-2.5 rounded-full shadow-lg ring-2 ring-slate-950 ${
              pnlPct >= 0 ? "bg-emerald-400" : "bg-rose-400"
            }`}
            style={{ left: `calc(${currentPos}% - 5px)` }}
            title={`Current: ${pnlPct.toFixed(2)}%`}
          />
        </div>

        <div className="mt-2 flex flex-wrap items-center justify-between gap-2 font-mono text-[11px] text-slate-400">
          <span>Peak SOL: {formatSol(pnl.peak_sol_value)} ({peakPct >= 0 ? "+" : ""}{peakPct.toFixed(2)}%)</span>
          {isWatching && pnl.retrace_low_sol !== null && (
            <span className="text-amber-300">
              Retrace Low: {formatSol(pnl.retrace_low_sol)} • Curl Trigger (+5%):{" "}
              {formatSol(pnl.retrace_low_sol * 1.05)}
            </span>
          )}
          {pnl.last_5m_buy_ratio !== null && (
            <span>
              5m Flow: <span className="text-emerald-400">{(pnl.last_5m_buy_ratio * 100).toFixed(0)}% buys</span> /{" "}
              <span className="text-rose-400">{((pnl.last_5m_sell_ratio ?? 0) * 100).toFixed(0)}% sells</span>
            </span>
          )}
        </div>
      </div>
    </section>
  );
}
