"use client";

import { ExternalLink, Radar, TrendingUp } from "lucide-react";
import { LiveJupiterPnl, TelemetryPayload, formatSol } from "@/lib/types";

interface Props {
  telemetry: TelemetryPayload | null;
}

function clampPct(value: number, min: number, max: number): number {
  return Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100));
}

function PositionCard({
  pnl,
  mint,
  symbol,
  isWatching,
}: {
  pnl: LiveJupiterPnl;
  mint: string;
  symbol: string;
  isWatching: boolean;
}) {
  const strategy = (pnl.strategy || "SCALPER").toUpperCase();
  const isWhaleScalp = strategy === "WHALE_SCALP";
  const partialTaken = Boolean(pnl.partial_tp_taken);
  const bankedSol = pnl.realized_partial_pnl_sol ?? 0;
  const basisSol =
    partialTaken && (pnl.remaining_cost_basis_sol ?? 0) > 0
      ? pnl.remaining_cost_basis_sol!
      : pnl.entry_sol;
  const pnlPct = pnl.pnl_pct;
  const peakPct =
    basisSol > 0 ? ((pnl.peak_sol_value - basisSol) / basisSol) * 100 : 0;

  const gaugeMin = -15;
  const baseMax = isWhaleScalp ? 20 : 25;
  const gaugeMax = Math.max(baseMax, Math.ceil((Math.max(peakPct, pnlPct) + 5) / 5) * 5);
  const hardStopPos = clampPct(-10, gaugeMin, gaugeMax);
  const zeroPos = clampPct(0, gaugeMin, gaugeMax);
  const beFloorPos = clampPct(isWhaleScalp ? 1 : 2, gaugeMin, gaugeMax);
  const trailArmLevel = isWhaleScalp ? 8 : 12;
  const trailArmPos = clampPct(trailArmLevel, gaugeMin, gaugeMax);
  const whaleTpPos = clampPct(15, gaugeMin, gaugeMax);
  const currentPos = clampPct(pnlPct, gaugeMin, gaugeMax);
  const peakPos = clampPct(peakPct, gaugeMin, gaugeMax);
  const tightTrailActive = isWhaleScalp && peakPct >= 12;
  const trailDropFactor = isWhaleScalp
    ? tightTrailActive
      ? 0.975
      : 0.965
    : partialTaken
      ? 0.9
      : 0.94;
  const trailStopPct = pnl.trail_armed
    ? ((pnl.peak_sol_value * trailDropFactor - basisSol) / Math.max(basisSol, 1e-9)) * 100
    : null;
  const trailStopPos = trailStopPct !== null ? clampPct(trailStopPct, gaugeMin, gaugeMax) : null;

  return (
    <div
      className={`rounded-xl border p-4 ${
        isWatching
          ? "border-amber-500/40 bg-amber-950/15"
          : isWhaleScalp
            ? "border-fuchsia-500/35 bg-fuchsia-950/15"
            : "border-emerald-500/35 bg-emerald-950/15"
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <TrendingUp
              className={`h-4 w-4 ${
                isWatching ? "text-amber-400" : isWhaleScalp ? "text-fuchsia-400" : "text-emerald-400"
              }`}
            />
            <a
              href={`https://dexscreener.com/solana/${mint}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-lg font-bold text-slate-100 hover:text-sky-300"
            >
              ${symbol}
              <ExternalLink className="h-3.5 w-3.5 text-slate-500" />
            </a>
            <span
              className={`rounded-full border px-2 py-0.5 font-mono text-[10px] font-semibold uppercase ${
                isWhaleScalp
                  ? "border-fuchsia-500/50 bg-fuchsia-500/20 text-fuchsia-300"
                  : "border-slate-700 bg-slate-800/80 text-slate-300"
              }`}
            >
              {strategy}
            </span>
            {partialTaken && (
              <span className="rounded-full border border-emerald-500/50 bg-emerald-500/20 px-2 py-0.5 font-mono text-[10px] font-semibold text-emerald-300">
                50% BANKED ({bankedSol >= 0 ? "+" : ""}
                {bankedSol.toFixed(4)} SOL)
              </span>
            )}
          </div>
          <div className="mt-0.5 font-mono text-[11px] text-slate-400">
            {mint.slice(0, 6)}...{mint.slice(-6)}
            {isWatching ? " • WATCHING LEG 2" : ""}
          </div>
        </div>

        <div className="grid grid-cols-3 gap-2 font-mono text-xs">
          <div className="rounded-lg border border-slate-800/80 bg-slate-950/70 px-2.5 py-1.5">
            <div className="text-[9px] uppercase text-slate-500">Entry</div>
            <div className="font-semibold tabular-nums text-slate-200">{formatSol(pnl.entry_sol)}</div>
          </div>
          <div className="rounded-lg border border-slate-800/80 bg-slate-950/70 px-2.5 py-1.5">
            <div className="text-[9px] uppercase text-slate-500">Exit</div>
            <div className="font-semibold tabular-nums text-slate-100">{formatSol(pnl.current_sol_value)}</div>
          </div>
          <div className="rounded-lg border border-slate-800/80 bg-slate-950/70 px-2.5 py-1.5">
            <div className="text-[9px] uppercase text-slate-500">PnL</div>
            <div
              className={`font-bold tabular-nums ${
                pnlPct > 0 ? "text-emerald-400" : pnlPct < 0 ? "text-rose-400" : "text-slate-300"
              }`}
            >
              {pnlPct > 0 ? "+" : ""}
              {pnlPct.toFixed(2)}%
            </div>
          </div>
        </div>
      </div>

      {/* Compact Risk Gauge */}
      <div className="mt-3 rounded-lg border border-slate-800/80 bg-slate-950/70 p-3">
        <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2 font-mono text-[10px]">
          <span className="text-slate-400">
            Peak {peakPct >= 0 ? "+" : ""}
            {peakPct.toFixed(1)}% ({formatSol(pnl.pnl_sol, true)})
          </span>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-rose-400">-10% SL</span>
            <span className={pnl.breakeven_armed ? "font-semibold text-cyan-300" : "text-slate-500"}>
              BE {isWhaleScalp ? "+5%/+1%" : "+8%/+2%"}
            </span>
            <span className={pnl.trail_armed ? "font-semibold text-amber-300" : "text-slate-500"}>
              Trail {isWhaleScalp ? "+8%/-3.5%" : partialTaken ? "+12%/-10%" : "+12%/-6%"}
            </span>
            {isWhaleScalp && <span className="text-emerald-400">+15% TP</span>}
          </div>
        </div>
        <div className="relative h-3 w-full overflow-hidden rounded-full bg-slate-900">
          <div className="absolute inset-y-0 left-0 bg-rose-500/25" style={{ width: `${hardStopPos}%` }} />
          <div className="absolute inset-y-0 right-0 bg-emerald-500/15" style={{ left: `${trailArmPos}%` }} />
          <div className="absolute inset-y-0 w-0.5 bg-rose-500" style={{ left: `${hardStopPos}%` }} />
          <div className="absolute inset-y-0 w-px bg-slate-600" style={{ left: `${zeroPos}%` }} />
          <div className="absolute inset-y-0 w-0.5 bg-cyan-400" style={{ left: `${beFloorPos}%` }} />
          <div className="absolute inset-y-0 w-0.5 bg-amber-400" style={{ left: `${trailArmPos}%` }} />
          {isWhaleScalp && (
            <div className="absolute inset-y-0 w-0.5 bg-emerald-400" style={{ left: `${whaleTpPos}%` }} />
          )}
          {trailStopPos !== null && (
            <div className="absolute inset-y-0 w-1 bg-amber-300" style={{ left: `${trailStopPos}%` }} />
          )}
          <div
            className="absolute top-0.5 h-2 w-1 rounded-full bg-slate-400/70"
            style={{ left: `calc(${peakPos}% - 2px)` }}
          />
          <div
            className={`absolute top-0 h-3 w-2 rounded-full ring-2 ring-slate-950 ${
              pnlPct >= 0 ? "bg-emerald-400" : "bg-rose-400"
            }`}
            style={{ left: `calc(${currentPos}% - 4px)` }}
          />
        </div>
      </div>
    </div>
  );
}

export function ActivePositionHero({ telemetry }: Props) {
  const focus = telemetry?.focus_state ?? "SCANNING";
  const maxChart = telemetry?.max_chart_positions ?? 3;
  const maxWhale = telemetry?.max_whale_positions ?? 3;

  // Prefer dual-lane arrays; fall back seamlessly to legacy single live_jupiter_pnl
  let chartPositions = telemetry?.chart_positions ?? [];
  let whalePositions = telemetry?.whale_positions ?? [];
  if (
    chartPositions.length === 0 &&
    whalePositions.length === 0 &&
    telemetry?.live_jupiter_pnl &&
    telemetry?.active_mint &&
    focus !== "SCANNING"
  ) {
    const legacy: LiveJupiterPnl = {
      ...telemetry.live_jupiter_pnl,
      mint: telemetry.active_mint,
      symbol: telemetry.active_symbol ?? "UNKNOWN",
    };
    if ((legacy.strategy || "").toUpperCase() === "WHALE_SCALP") {
      whalePositions = [legacy];
    } else {
      chartPositions = [legacy];
    }
  }

  const totalOpen = chartPositions.length + whalePositions.length;

  if (totalOpen === 0) {
    return (
      <section className="rounded-2xl border border-slate-800/80 bg-slate-900/50 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-blue-500/30 bg-blue-500/10 text-blue-400">
              <Radar className="h-5 w-5" />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-slate-200">
                No Active Positions (0 / {maxChart + maxWhale})
              </h2>
              <p className="text-xs text-slate-400">
                Dual-Lane Engine ready — Chart Positions (0 / {maxChart}) • Whale Positions (0 / {maxWhale})
              </p>
            </div>
          </div>
          <div className="font-mono text-xs text-slate-500">
            Cooldowns active: {telemetry?.active_cooldowns?.length ?? 0}
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="space-y-4 rounded-2xl border border-slate-800/80 bg-slate-900/50 p-5">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800/80 pb-3">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-slate-100">Active Positions</h2>
          <span className="rounded-full border border-emerald-500/40 bg-emerald-500/15 px-2.5 py-0.5 font-mono text-[11px] font-semibold text-emerald-300">
            Chart Positions ({chartPositions.length} / {maxChart})
          </span>
          <span className="rounded-full border border-fuchsia-500/40 bg-fuchsia-500/15 px-2.5 py-0.5 font-mono text-[11px] font-semibold text-fuchsia-300">
            Whale Positions ({whalePositions.length} / {maxWhale})
          </span>
        </div>
        <span className="font-mono text-xs text-slate-400">
          {totalOpen} / {maxChart + maxWhale} slots active
        </span>
      </div>

      {chartPositions.length > 0 && (
        <div className="space-y-2.5">
          <div className="font-mono text-[11px] uppercase tracking-wider text-emerald-300">
            Chart Positions ({chartPositions.length} / {maxChart})
          </div>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {chartPositions.map((p, idx) => (
              <PositionCard
                key={`chart-${p.mint || idx}`}
                pnl={p}
                mint={p.mint || telemetry?.active_mint || ""}
                symbol={p.symbol || telemetry?.active_symbol || "UNKNOWN"}
                isWatching={focus === "WATCHING_REENTRY" && idx === 0}
              />
            ))}
          </div>
        </div>
      )}

      {whalePositions.length > 0 && (
        <div className="space-y-2.5">
          <div className="font-mono text-[11px] uppercase tracking-wider text-fuchsia-300">
            Whale Positions ({whalePositions.length} / {maxWhale})
          </div>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {whalePositions.map((p, idx) => (
              <PositionCard
                key={`whale-${p.mint || idx}`}
                pnl={p}
                mint={p.mint || ""}
                symbol={p.symbol || "WHALE"}
                isWatching={false}
              />
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
