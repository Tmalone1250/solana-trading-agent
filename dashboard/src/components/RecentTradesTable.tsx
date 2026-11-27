"use client";

import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, ExternalLink } from "lucide-react";
import {
  ClosedTrade,
  ClosedTradesSummary,
  formatShortTime,
  formatSol,
} from "@/lib/types";

const PAGE_SIZE = 7;

interface Props {
  trades: ClosedTrade[];
  summary?: ClosedTradesSummary;
}

function getWinRateBadgeStyle(winRatePct: number | null, knownTrades: number): string {
  if (knownTrades === 0 || winRatePct === null) {
    return "border-slate-700 bg-slate-900 text-slate-400";
  }
  if (winRatePct >= 50) {
    return "border-emerald-500/40 bg-emerald-500/15 text-emerald-300";
  }
  if (winRatePct >= 40) {
    return "border-amber-500/40 bg-amber-500/15 text-amber-300";
  }
  return "border-rose-500/40 bg-rose-500/15 text-rose-300";
}

export function RecentTradesTable({ trades, summary }: Props) {
  const [page, setPage] = useState(1);

  const totalPages = Math.max(1, Math.ceil(trades.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  const startIdx = (safePage - 1) * PAGE_SIZE;
  const pageTrades = trades.slice(startIdx, startIdx + PAGE_SIZE);

  const knownFallback = trades.filter(
    (t) => t.net_pnl_known !== false && t.pnl_sol !== null,
  );
  const totalTrades = summary ? summary.total_trades : trades.length;
  const knownTrades = summary?.known_trades ?? knownFallback.length;
  const unknownTrades = summary?.unknown_trades ?? Math.max(0, totalTrades - knownTrades);
  const wins = summary
    ? summary.wins
    : knownFallback.filter((t) => (t.pnl_sol ?? 0) > 0).length;
  const losses = summary
    ? summary.losses
    : knownFallback.filter((t) => (t.pnl_sol ?? 0) <= 0).length;
  const winRatePct = summary
    ? summary.win_rate_pct
    : knownTrades > 0
      ? Number(((wins / knownTrades) * 100).toFixed(1))
      : totalTrades === 0
        ? 0.0
        : null;
  const wrStyle = getWinRateBadgeStyle(winRatePct, knownTrades);

  return (
    <section className="rounded-2xl border border-slate-800/80 bg-slate-900/60">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800/80 px-5 py-3.5">
        <div>
          <h2 className="text-sm font-semibold text-slate-100">
            Recent Closed Trades
          </h2>
          <p className="text-xs text-slate-400">
            Session history from DuckDB (up to 100 newest-first)
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`rounded-full border px-2.5 py-0.5 font-mono text-xs font-semibold ${wrStyle}`}
          >
            {wins}W / {losses}L •{" "}
            {winRatePct !== null ? `${winRatePct.toFixed(1)}% WR` : "UNKNOWN WR"}
          </span>
          <span className="rounded-full border border-slate-700 bg-slate-950 px-2.5 py-0.5 font-mono text-xs text-slate-300">
            {totalTrades} trades
            {unknownTrades > 0 ? ` (${unknownTrades} unknown)` : ""}
          </span>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-left text-xs">
          <thead>
            <tr className="border-b border-slate-800/80 bg-slate-950/50 font-mono text-[10px] uppercase tracking-wider text-slate-400">
              <th className="px-4 py-3">Token</th>
              <th className="px-4 py-3">Strategy</th>
              <th className="px-4 py-3">Entry SOL</th>
              <th className="px-4 py-3">Exit SOL</th>
              <th className="px-4 py-3">PnL (SOL / %)</th>
              <th className="px-4 py-3">Exit Reason</th>
              <th className="px-4 py-3 text-right">Closed</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/60 font-mono">
            {pageTrades.length === 0 ? (
              <tr>
                <td
                  colSpan={7}
                  className="px-4 py-8 text-center text-xs text-slate-500"
                >
                  No closed trades recorded yet.
                </td>
              </tr>
            ) : (
              pageTrades.map((t, idx) => {
                const isKnownNet = t.net_pnl_known !== false && t.pnl_sol !== null;
                const win = isKnownNet && (t.pnl_sol ?? 0) > 0;
                const loss = isKnownNet && (t.pnl_sol ?? 0) < 0;
                const pnlColor = !isKnownNet
                  ? "text-amber-300"
                  : win
                    ? "text-emerald-400"
                    : loss
                      ? "text-rose-400"
                      : "text-slate-300";
                const strat = (t.strategy || "SCALPER").toUpperCase();
                const isWhale = strat === "WHALE_SCALP";

                return (
                  <tr
                    key={`${t.mint}-${t.closed_at}-${startIdx + idx}`}
                    className="hover:bg-slate-800/30"
                  >
                    <td className="px-4 py-3">
                      <a
                        href={`https://dexscreener.com/solana/${t.mint}`}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 font-sans font-semibold text-slate-100 hover:text-sky-300"
                      >
                        ${t.symbol}
                        <ExternalLink className="h-3 w-3 text-slate-500" />
                      </a>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex rounded border px-1.5 py-0.5 text-[10px] font-semibold ${
                          isWhale
                            ? "border-fuchsia-500/50 bg-fuchsia-500/20 text-fuchsia-300"
                            : "border-slate-700 bg-slate-800/70 text-slate-300"
                        }`}
                      >
                        {strat}
                      </span>
                    </td>
                    <td className="px-4 py-3 tabular-nums text-slate-300">
                      {t.entry_sol !== null ? formatSol(t.entry_sol) : "UNKNOWN"}
                    </td>
                    <td className="px-4 py-3 tabular-nums text-slate-200">
                      {formatSol(t.exit_sol)}
                    </td>
                    <td
                      className={`px-4 py-3 font-semibold tabular-nums ${pnlColor}`}
                    >
                      {isKnownNet && t.pnl_sol !== null ? (
                        <>
                          {formatSol(t.pnl_sol, true)}{" "}
                          <span className="text-[11px] font-normal">
                            ({(t.pnl_pct ?? 0) > 0 ? "+" : ""}
                            {(t.pnl_pct ?? 0).toFixed(2)}%)
                          </span>
                        </>
                      ) : (
                        <span title={t.gross_pnl_sol !== undefined && t.gross_pnl_sol !== null ? `Incomplete gross: ${formatSol(t.gross_pnl_sol, true)}` : "Fees or entry unknown"}>
                          UNKNOWN
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className="rounded border border-slate-700/80 bg-slate-950 px-2 py-0.5 text-[11px] text-slate-300">
                        {t.exit_reason}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right text-slate-400">
                      {formatShortTime(t.closed_at)}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {trades.length > 0 && (
        <div className="flex items-center justify-between border-t border-slate-800/80 px-5 py-2.5 font-mono text-xs text-slate-400">
          <span>
            Page {safePage} of {totalPages}
          </span>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={safePage <= 1}
              className="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-950 px-2.5 py-1 text-[11px] text-slate-200 transition hover:border-slate-500 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              Prev
            </button>
            <button
              type="button"
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={safePage >= totalPages}
              className="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-950 px-2.5 py-1 text-[11px] text-slate-200 transition hover:border-slate-500 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Next
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
