"use client";

import { ExternalLink } from "lucide-react";
import { ClosedTrade, formatShortTime, formatSol } from "@/lib/types";

interface Props {
  trades: ClosedTrade[];
}

export function RecentTradesTable({ trades }: Props) {
  return (
    <section className="rounded-2xl border border-slate-800/80 bg-slate-900/60">
      <div className="flex items-center justify-between border-b border-slate-800/80 px-5 py-3.5">
        <div>
          <h2 className="text-sm font-semibold text-slate-100">
            Recent Closed Trades
          </h2>
          <p className="text-xs text-slate-400">Last 15 closed positions from DuckDB</p>
        </div>
        <span className="rounded-full border border-slate-700 bg-slate-950 px-2.5 py-0.5 font-mono text-xs text-slate-300">
          {trades.length} trades
        </span>
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
            {trades.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-xs text-slate-500">
                  No closed trades recorded yet.
                </td>
              </tr>
            ) : (
              trades.map((t, idx) => {
                const win = t.pnl_sol > 0;
                const loss = t.pnl_sol < 0;
                const pnlColor = win
                  ? "text-emerald-400"
                  : loss
                    ? "text-rose-400"
                    : "text-slate-300";
                const strat = (t.strategy || "SCALPER").toUpperCase();
                const isWhale = strat === "WHALE_SCALP";

                return (
                  <tr key={`${t.mint}-${t.closed_at}-${idx}`} className="hover:bg-slate-800/30">
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
                        className={`inline-flex rounded border px-1.5 py-0.5 text-[10px] font-semibold ${isWhale
                          ? "border-fuchsia-500/50 bg-fuchsia-500/20 text-fuchsia-300"
                          : "border-slate-700 bg-slate-800/70 text-slate-300"
                          }`}
                      >
                        {strat}
                      </span>
                    </td>
                    <td className="px-4 py-3 tabular-nums text-slate-300">
                      {formatSol(t.entry_sol)}
                    </td>
                    <td className="px-4 py-3 tabular-nums text-slate-200">
                      {formatSol(t.exit_sol)}
                    </td>
                    <td className={`px-4 py-3 font-semibold tabular-nums ${pnlColor}`}>
                      {formatSol(t.pnl_sol, true)}{" "}
                      <span className="text-[11px] font-normal">
                        ({t.pnl_pct > 0 ? "+" : ""}
                        {t.pnl_pct.toFixed(2)}%)
                      </span>
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
    </section>
  );
}
