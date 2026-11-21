"use client";

import { ExternalLink, ShieldCheck } from "lucide-react";
import { BreakoutTelemetry, WatchlistSlot, formatUsdCompact } from "@/lib/types";

interface Props {
  slots: WatchlistSlot[];
}

function getBreakoutBarStyle(state: string): { bar: string; text: string; badge: string } {
  if (state === "IGNITION") {
    return {
      bar: "bg-emerald-400",
      text: "text-emerald-300",
      badge: "border-emerald-500/40 bg-emerald-500/15 text-emerald-300",
    };
  }
  if (state === "COILING") {
    return {
      bar: "bg-amber-400",
      text: "text-amber-300",
      badge: "border-amber-500/40 bg-amber-500/15 text-amber-300",
    };
  }
  if (state === "EXTENDED") {
    return {
      bar: "bg-rose-500",
      text: "text-rose-300",
      badge: "border-rose-500/40 bg-rose-500/15 text-rose-300",
    };
  }
  return {
    bar: "bg-slate-500",
    text: "text-slate-300",
    badge: "border-slate-700 bg-slate-800/70 text-slate-400",
  };
}

function BreakoutMeter({ bt }: { bt?: BreakoutTelemetry | null }) {
  if (!bt) {
    return <span className="font-mono text-[10px] text-slate-500">Awaiting OHLCV...</span>;
  }
  const st = getBreakoutBarStyle(bt.state);
  const tfShort = bt.timeframe === "1m_fallback" ? "1m" : bt.timeframe;
  const passed = new Set(bt.passed_gates ?? []);
  const extSign = bt.ema_ext_pct >= 0 ? "+" : "";
  const pills = [
    { key: "COIL", label: "COIL", ok: passed.has("COIL") },
    { key: "RES", label: "RES", ok: passed.has("RES") },
    { key: "VOL", label: `VOL ${bt.vol_ratio.toFixed(1)}x`, ok: passed.has("VOL") },
    { key: "EMA", label: "EMA", ok: passed.has("EMA") && passed.has("VWAP") },
    { key: "EXT", label: `EXT ${extSign}${bt.ema_ext_pct.toFixed(1)}%`, ok: passed.has("EXT") },
  ];
  const tooltip = bt.failed_gates?.length ? `Failed: ${bt.failed_gates.join(" • ")}` : "All breakout gates passed";

  return (
    <div className="w-56" title={tooltip}>
      <div className="flex items-center justify-between gap-1 font-mono text-[10px]">
        <span className={`font-semibold ${st.text}`}>
          {bt.score}/100
        </span>
        <span className={`rounded border px-1.5 py-0.25 text-[9px] font-semibold ${st.badge}`}>
          {bt.state} • {tfShort}
        </span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
        <div
          className={`h-full rounded-full transition-all ${st.bar}`}
          style={{ width: `${Math.min(100, Math.max(0, bt.score))}%` }}
        />
      </div>
      <div className="mt-1.5 flex flex-nowrap items-center gap-1 overflow-x-auto font-mono text-[9px]">
        {pills.map((p) => (
          <span
            key={p.key}
            className={`whitespace-nowrap rounded border px-1 py-0.25 ${
              p.ok
                ? "border-emerald-500/35 bg-emerald-500/15 text-emerald-300"
                : "border-rose-500/25 bg-rose-500/10 text-rose-300/80"
            }`}
          >
            {p.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function getLeg2Color(score: number): string {
  if (score >= 80) return "bg-emerald-400";
  if (score >= 70) return "bg-cyan-400";
  if (score >= 50) return "bg-amber-400";
  return "bg-slate-500";
}

function getSetupBadge(setup: string): string {
  if (setup === "SWEEP_RECLAIM" || setup === "BREAKOUT_IGNITION") {
    return "border-emerald-500/40 bg-emerald-500/15 text-emerald-300";
  }
  if (setup === "DISPLACEMENT_RETEST") {
    return "border-cyan-500/40 bg-cyan-500/15 text-cyan-300";
  }
  return "border-slate-700 bg-slate-800/70 text-slate-400";
}

function getStatusBadge(slot: WatchlistSlot): { label: string; cls: string } {
  const status = slot.status ?? (slot.scores?.approved ? "APPROVED" : slot.scalper_eligible ? "READY" : "BLOCKED");
  if (status === "APPROVED") {
    return { label: "APPROVED", cls: "border-emerald-500/50 bg-emerald-500/20 text-emerald-300" };
  }
  if (status === "READY") {
    return { label: "READY", cls: "border-sky-500/40 bg-sky-500/15 text-sky-300" };
  }
  if (status === "SOAKING") {
    return { label: "SOAKING", cls: "border-amber-500/40 bg-amber-500/15 text-amber-300" };
  }
  return { label: "BLOCKED", cls: "border-rose-500/40 bg-rose-500/15 text-rose-300" };
}

export function WatchlistTable({ slots }: Props) {
  return (
    <section className="rounded-2xl border border-slate-800/80 bg-slate-900/60">
      <div className="flex items-center justify-between border-b border-slate-800/80 px-5 py-3.5">
        <div>
          <h2 className="text-sm font-semibold text-slate-100">
            5-Slot Rotating Watchlist
          </h2>
          <p className="text-xs text-slate-400">
            3m mandatory soak • 5m OHLCV Scalper Engine • Leg 2 Predictor • Decision 1
          </p>
        </div>
        <span className="rounded-full border border-slate-700 bg-slate-950 px-2.5 py-0.5 font-mono text-xs text-slate-300">
          {slots.length} / 5 slots
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-left text-xs">
          <thead>
            <tr className="border-b border-slate-800/80 bg-slate-950/50 font-mono text-[10px] uppercase tracking-wider text-slate-400">
              <th className="px-4 py-3">Token (MC / Liq)</th>
              <th className="px-4 py-3">Soak (3m min)</th>
              <th className="px-4 py-3">Scalper Engine</th>
              <th className="px-4 py-3">Breakout Meter</th>
              <th className="px-4 py-3">Leg 2 Score</th>
              <th className="px-4 py-3">Decision 1</th>
              <th className="px-4 py-3 text-right">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/60">
            {slots.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center font-mono text-xs text-slate-500">
                  No tokens currently in active_watchlist — awaiting next DexScreener / GeckoTerminal scan...
                </td>
              </tr>
            ) : (
              slots.map((slot) => {
                const soakPct = Math.min(100, (slot.time_in_slot_minutes / 3.0) * 100);
                const soaked = slot.time_in_slot_minutes >= 3.0;
                const sc = slot.scores;
                const distPct =
                  slot.dist_from_1h_high_pct !== null && slot.dist_from_1h_high_pct !== undefined
                    ? slot.dist_from_1h_high_pct * 100
                    : null;
                const badge = getStatusBadge(slot);
                const strikes = slot.vamp_strikes ?? 0;

                return (
                  <tr key={slot.mint} className="transition hover:bg-slate-800/30">
                    {/* Token + MC/Liq */}
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-1.5">
                        <a
                          href={`https://dexscreener.com/solana/${slot.mint}`}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 font-semibold text-slate-100 hover:text-sky-300"
                        >
                          ${slot.symbol}
                          <ExternalLink className="h-3 w-3 text-slate-500" />
                        </a>
                        {slot.safety_passed && (
                          <span title="RugCheck + Jupiter Shield cached pass">
                            <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 font-mono text-[11px] text-slate-400">
                        MC {formatUsdCompact(slot.market_cap_usd || slot.peak_mc)} / Liq{" "}
                        {formatUsdCompact(slot.liquidity_usd)}
                      </div>
                    </td>

                    {/* Soak Progress */}
                    <td className="px-4 py-3">
                      <div className="w-28">
                        <div className="flex justify-between font-mono text-[11px]">
                          <span className={soaked ? "text-emerald-400" : "text-amber-300"}>
                            {slot.time_in_slot_minutes.toFixed(1)}m
                          </span>
                          <span className="text-slate-500">/ 3.0m</span>
                        </div>
                        <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                          <div
                            className={`h-full rounded-full ${soaked ? "bg-emerald-400" : "bg-amber-400"}`}
                            style={{ width: `${soakPct}%` }}
                          />
                        </div>
                      </div>
                    </td>

                    {/* Scalper Engine */}
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span
                          className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold ${getSetupBadge(
                            slot.setup_type
                          )}`}
                        >
                          {slot.setup_type}
                        </span>
                      </div>
                      <div className="mt-1 font-mono text-[11px] text-slate-400">
                        RSI{" "}
                        <span
                          className={
                            slot.rsi_5m !== null && slot.rsi_5m > 70
                              ? "font-semibold text-rose-400"
                              : slot.rsi_5m !== null && slot.rsi_5m >= 42 && slot.rsi_5m <= 65
                                ? "text-emerald-400"
                                : "text-slate-200"
                          }
                        >
                          {slot.rsi_5m !== null ? slot.rsi_5m.toFixed(1) : "—"}
                        </span>{" "}
                        • 1hHi{" "}
                        <span
                          className={
                            distPct !== null && distPct < 5
                              ? "font-semibold text-rose-400"
                              : "text-slate-200"
                          }
                        >
                          {distPct !== null ? `-${distPct.toFixed(1)}%` : "—"}
                        </span>
                      </div>
                    </td>

                    {/* Breakout Meter */}
                    <td className="px-4 py-3">
                      <BreakoutMeter bt={slot.breakout_telemetry ?? slot.scores?.breakout_telemetry} />
                    </td>

                    {/* Leg 2 Score Bar */}
                    <td className="px-4 py-3">
                      <div className="w-28">
                        <div className="flex justify-between font-mono text-[11px]">
                          <span className="font-semibold text-slate-200">
                            {slot.leg2_score.toFixed(0)}
                          </span>
                          <span className="text-slate-500">/ 100</span>
                        </div>
                        <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                          <div
                            className={`h-full rounded-full ${getLeg2Color(slot.leg2_score)}`}
                            style={{ width: `${Math.min(100, Math.max(0, slot.leg2_score))}%` }}
                          />
                        </div>
                      </div>
                    </td>

                    {/* Decision 1 */}
                    <td className="px-4 py-3 font-mono text-[11px]">
                      {sc ? (
                        <div>
                          <div className="flex items-center gap-1.5">
                            <span
                              className={`font-semibold ${
                                sc.action === "BUY"
                                  ? "text-emerald-400"
                                  : sc.action === "WATCH"
                                    ? "text-amber-300"
                                    : "text-rose-400"
                              }`}
                            >
                              {sc.action}
                            </span>
                            {sc.action_confidence !== undefined && (
                              <span className="text-slate-500">
                                ({(sc.action_confidence * 100).toFixed(0)}%)
                              </span>
                            )}
                            {strikes > 0 && (
                              <span className="rounded border border-amber-500/40 bg-amber-500/15 px-1 text-[10px] text-amber-300">
                                {strikes}/2 strike
                              </span>
                            )}
                          </div>
                          <div className="mt-0.5 text-slate-400">
                            N:{sc.narrative_strength?.toFixed(1) ?? "—"} M:
                            {sc.momentum_quality?.toFixed(1) ?? "—"} V:
                            <span
                              className={
                                (sc.is_copycat_vamp ?? 0) >= 0.75
                                  ? "text-rose-400"
                                  : (sc.is_copycat_vamp ?? 0) > 0.55
                                    ? "text-amber-300"
                                    : "text-slate-300"
                              }
                            >
                              {sc.is_copycat_vamp?.toFixed(2) ?? "—"}
                            </span>
                          </div>
                        </div>
                      ) : (
                        <span className="text-slate-500">Pending eval...</span>
                      )}
                    </td>

                    {/* Status */}
                    <td className="px-4 py-3 text-right">
                      <span
                        className={`inline-flex rounded-md border px-2 py-0.5 font-mono text-[10px] font-semibold ${badge.cls}`}
                      >
                        {badge.label}
                      </span>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
