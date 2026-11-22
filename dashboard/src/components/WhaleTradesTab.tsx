"use client";

import { ExternalLink } from "lucide-react";
import { WhaleStats, WhaleTrade, formatSol, truncateAddress } from "@/lib/types";

function formatHoldDuration(seconds?: number | null): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

function getExitReasonStyle(reason: string): string {
  const r = reason.toUpperCase();
  if (r === "OPEN") return "border-fuchsia-500/50 bg-fuchsia-500/20 text-fuchsia-300";
  if (r === "WHALE_FRONT_RUN_TP") return "border-emerald-500/50 bg-emerald-500/20 text-emerald-300";
  if (r === "WHALE_PEAK_TRAIL" || r === "WHALE_BREAKEVEN_SHIELD") {
    return "border-cyan-500/40 bg-cyan-500/15 text-cyan-300";
  }
  if (r === "WHALE_SHADOW_EXIT") return "border-amber-500/50 bg-amber-500/20 text-amber-300";
  if (r === "WHALE_HARD_STOP") return "border-rose-500/50 bg-rose-500/20 text-rose-300";
  if (r === "WHALE_TIME_STOP") return "border-slate-600 bg-slate-800/80 text-slate-300";
  return "border-slate-700 bg-slate-900 text-slate-300";
}

interface Props {
  whaleTrades: WhaleTrade[];
  whaleStats?: WhaleStats;
  formatRelativeTime: (iso?: string | null) => string;
}

export function WhaleTradesTab({ whaleTrades, whaleStats, formatRelativeTime }: Props) {
  return (
    <div>
      <div className="grid grid-cols-2 gap-2 border-b border-slate-800/80 bg-slate-950/30 px-4 py-2.5 font-mono sm:flex sm:flex-wrap sm:items-center sm:gap-4">
        <div className="flex items-center gap-1.5 text-[11px]">
          <span className="text-slate-500">Win Rate:</span>
          <span className="font-semibold tabular-nums text-emerald-300">
            {(whaleStats?.win_rate_pct ?? 0).toFixed(1)}%
          </span>
          <span className="text-slate-600">
            ({whaleStats?.wins ?? 0}W/{whaleStats?.losses ?? 0}L)
          </span>
        </div>
        <div className="flex items-center gap-1.5 text-[11px]">
          <span className="text-slate-500">Whale PnL:</span>
          <span
            className={`font-semibold tabular-nums ${(whaleStats?.total_pnl_sol ?? 0) > 0
              ? "text-emerald-400"
              : (whaleStats?.total_pnl_sol ?? 0) < 0
                ? "text-rose-400"
                : "text-slate-300"
              }`}
          >
            {formatSol(whaleStats?.total_pnl_sol ?? 0, true)}
          </span>
        </div>
        <div className="flex items-center gap-1.5 text-[11px]">
          <span className="text-slate-500">Avg Hold:</span>
          <span className="font-semibold tabular-nums text-slate-200">
            {formatHoldDuration(whaleStats?.avg_hold_seconds ?? 0)}
          </span>
        </div>
        <div className="flex items-center gap-1.5 text-[11px]">
          <span className="text-slate-500">Trades:</span>
          <span className="font-semibold tabular-nums text-slate-200">
            {whaleStats?.total_trades ?? 0}
          </span>
        </div>
      </div>

      <div className="terminal-scroll max-h-[340px] overflow-x-auto overflow-y-auto">
        <table className="w-full border-collapse text-left text-xs">
          <thead className="sticky top-0 z-10 bg-slate-950 shadow-[0_1px_0_0_rgba(30,41,59,0.9)]">
            <tr className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
              <th className="px-4 py-2.5">Token / Mint</th>
              <th className="px-4 py-2.5">Triggering Whale(s)</th>
              <th className="px-4 py-2.5">Entry → Exit SOL</th>
              <th className="px-4 py-2.5">PnL (SOL / %)</th>
              <th className="px-4 py-2.5">Hold</th>
              <th className="px-4 py-2.5">Exit Reason</th>
              <th className="px-4 py-2.5 text-right">Time</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/60 font-mono">
            {whaleTrades.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-xs text-slate-500">
                  No WHALE_SCALP trades yet — qualifying fast-track entries will appear here live.
                </td>
              </tr>
            ) : (
              whaleTrades.map((t, idx) => {
                const isOpen = t.status === "OPEN";
                const pnlColor =
                  t.pnl_sol > 0 ? "text-emerald-400" : t.pnl_sol < 0 ? "text-rose-400" : "text-slate-300";
                return (
                  <tr
                    key={`${t.mint}-${t.closed_at || t.opened_at || idx}`}
                    className={isOpen ? "bg-fuchsia-950/15 hover:bg-fuchsia-950/25" : "hover:bg-slate-800/30"}
                  >
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <a
                          href={`https://dexscreener.com/solana/${t.mint}`}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 font-sans font-semibold text-slate-100 hover:text-sky-300"
                        >
                          ${t.symbol}
                          <ExternalLink className="h-3 w-3 text-slate-500" />
                        </a>
                        {t.partial_tp_taken && (
                          <span className="rounded-full border border-emerald-500/50 bg-emerald-500/20 px-1.5 py-0.5 font-mono text-[9px] font-semibold text-emerald-300">
                            50% BANKED ({(t.realized_partial_pnl_sol ?? 0) >= 0 ? "+" : ""}
                            {(t.realized_partial_pnl_sol ?? 0).toFixed(4)} SOL)
                          </span>
                        )}
                      </div>
                      <div className="text-[10px] text-slate-500">{truncateAddress(t.mint)}</div>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap gap-1">
                        {t.triggering_wallets.length === 0 ? (
                          <span className="text-slate-500">—</span>
                        ) : (
                          t.triggering_wallets.map((w) => (
                            <a
                              key={w.wallet}
                              href={`https://solscan.io/account/${w.wallet}`}
                              target="_blank"
                              rel="noreferrer"
                              className="inline-flex items-center gap-0.5 rounded border border-slate-700/80 bg-slate-950 px-1.5 py-0.5 font-sans text-[10px] font-medium text-cyan-300 hover:border-cyan-500/60"
                            >
                              {w.label}
                              <ExternalLink className="h-2.5 w-2.5 text-slate-500" />
                            </a>
                          ))
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3 tabular-nums text-slate-200">
                      {t.entry_sol.toFixed(3)} → {t.exit_sol.toFixed(3)}
                    </td>
                    <td className={`px-4 py-3 font-semibold tabular-nums ${pnlColor}`}>
                      {formatSol(t.pnl_sol, true)}{" "}
                      <span className="text-[11px] font-normal">
                        ({t.pnl_pct > 0 ? "+" : ""}
                        {t.pnl_pct.toFixed(1)}%)
                      </span>
                    </td>
                    <td className="px-4 py-3 tabular-nums text-slate-300">
                      {formatHoldDuration(t.hold_seconds)}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex rounded border px-1.5 py-0.5 text-[10px] font-semibold ${getExitReasonStyle(
                          t.exit_reason
                        )}`}
                      >
                        {t.exit_reason}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right text-slate-400">
                      {formatRelativeTime(t.closed_at || t.opened_at)}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
