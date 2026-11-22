"use client";

import { useState } from "react";
import { Check, Copy, ExternalLink, Loader2, Search, X } from "lucide-react";
import { formatSol, formatShortTime, truncateAddress } from "@/lib/types";

export interface InsiderCandidate {
  wallet: string;
  wallet_label?: string;
  total_sol_spent: number;
  buy_count: number;
  first_buy_ts: number;
  first_buy_utc: string;
  mins_before_breakout: number;
  already_in_roster: boolean;
  gmgn_url: string;
  solscan_url: string;
}

export interface InsiderScanResponse {
  mint: string;
  symbol: string;
  pool_address: string;
  breakout_ts: number;
  breakout_utc: string;
  lookback_mins: number;
  min_sol: number;
  pages_scanned: number;
  total_candidates: number;
  candidates: InsiderCandidate[];
}

interface Props {
  isOpen: boolean;
  onClose: () => void;
  agentUrl: string;
}

function parseBreakoutInput(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  if (/^\d+$/.test(trimmed)) {
    const val = Number(trimmed);
    return val > 1e12 ? Math.floor(val / 1000) : val;
  }
  const parsedMs = Date.parse(trimmed);
  if (!Number.isNaN(parsedMs) && parsedMs > 0) {
    return Math.floor(parsedMs / 1000);
  }
  return null;
}

export function InsiderScannerModal({ isOpen, onClose, agentUrl }: Props) {
  const [mintInput, setMintInput] = useState("");
  const [breakoutInput, setBreakoutInput] = useState("");
  const [lookbackMins, setLookbackMins] = useState("30");
  const [minSol, setMinSol] = useState("1.0");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<InsiderScanResponse | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  if (!isOpen) return null;

  function copyText(text: string, key: string) {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      navigator.clipboard.writeText(text).catch(() => {});
      setCopiedKey(key);
      setTimeout(() => setCopiedKey((prev) => (prev === key ? null : prev)), 1600);
    }
  }

  async function handleRunScan(e: React.FormEvent) {
    e.preventDefault();
    const cleanMint = mintInput.trim();
    if (!cleanMint || loading) return;

    setLoading(true);
    setError(null);
    try {
      const breakoutTs = parseBreakoutInput(breakoutInput);
      const lookbackVal = Math.max(1, Math.min(240, Number(lookbackMins) || 30));
      const minSolVal = Math.max(0.05, Number(minSol) || 1.0);

      const res = await fetch(`${agentUrl}/api/insider-scan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mint: cleanMint,
          breakout_ts: breakoutTs,
          lookback_mins: lookbackVal,
          min_sol: minSolVal,
        }),
      });
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody.detail || `HTTP ${res.status}`);
      }
      const data: InsiderScanResponse = await res.json();
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Scan failed");
    } finally {
      setLoading(false);
    }
  }

  const newWallets = (result?.candidates ?? [])
    .filter((c) => !c.already_in_roster)
    .map((c) => c.wallet);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm">
      <div className="flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-cyan-500/35 bg-slate-900 shadow-2xl">
        {/* Modal Header */}
        <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3.5">
          <div>
            <h3 className="font-mono text-sm font-semibold text-slate-100">
              Insider Wallet Scanner
            </h3>
            <p className="text-xs text-slate-400">
              Scan Helius pool swaps before a runner&apos;s breakout candle to discover smart-money wallets
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="cursor-pointer rounded-lg border border-slate-800 bg-slate-950/80 p-1.5 text-slate-400 hover:border-slate-700 hover:text-slate-200"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Form Controls */}
        <form onSubmit={handleRunScan} className="border-b border-slate-800/80 bg-slate-950/50 p-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-12">
            <div className="sm:col-span-5">
              <label className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-slate-400">
                Token CA / Pool Address *
              </label>
              <input
                type="text"
                required
                value={mintInput}
                onChange={(e) => setMintInput(e.target.value)}
                placeholder="Paste Solana token mint or pool CA..."
                className="w-full rounded-lg border border-slate-800 bg-slate-900 px-3 py-1.5 font-mono text-xs text-slate-100 placeholder-slate-500 focus:border-cyan-500 focus:outline-none"
              />
            </div>

            <div className="sm:col-span-3">
              <label className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-slate-400">
                Breakout Time (Optional)
              </label>
              <input
                type="text"
                value={breakoutInput}
                onChange={(e) => setBreakoutInput(e.target.value)}
                placeholder="Auto-detect volume spike"
                className="w-full rounded-lg border border-slate-800 bg-slate-900 px-3 py-1.5 font-mono text-xs text-slate-100 placeholder-slate-500 focus:border-cyan-500 focus:outline-none"
              />
            </div>

            <div className="grid grid-cols-2 gap-2 sm:col-span-2">
              <div>
                <label className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-slate-400">
                  Lookback (m)
                </label>
                <input
                  type="number"
                  min={1}
                  max={240}
                  value={lookbackMins}
                  onChange={(e) => setLookbackMins(e.target.value)}
                  className="w-full rounded-lg border border-slate-800 bg-slate-900 px-2 py-1.5 font-mono text-xs text-slate-100 focus:border-cyan-500 focus:outline-none"
                />
              </div>
              <div>
                <label className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-slate-400">
                  Min SOL
                </label>
                <input
                  type="number"
                  step="0.1"
                  min={0.05}
                  value={minSol}
                  onChange={(e) => setMinSol(e.target.value)}
                  className="w-full rounded-lg border border-slate-800 bg-slate-900 px-2 py-1.5 font-mono text-xs text-slate-100 focus:border-cyan-500 focus:outline-none"
                />
              </div>
            </div>

            <div className="flex items-end sm:col-span-2">
              <button
                type="submit"
                disabled={loading || !mintInput.trim()}
                className="inline-flex w-full cursor-pointer items-center justify-center gap-1.5 rounded-lg border border-cyan-500/50 bg-cyan-500/20 px-3 py-1.5 font-mono text-xs font-semibold text-cyan-200 transition hover:bg-cyan-500/30 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {loading ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    Scanning...
                  </>
                ) : (
                  <>
                    <Search className="h-3.5 w-3.5" />
                    Run Scan
                  </>
                )}
              </button>
            </div>
          </div>

          {error && (
            <div className="mt-2.5 rounded-lg border border-rose-500/40 bg-rose-950/30 px-3 py-1.5 font-mono text-xs text-rose-300">
              {error}
            </div>
          )}
        </form>

        {/* Results Header & Table */}
        <div className="flex-1 overflow-y-auto p-4">
          {!result ? (
            <div className="py-12 text-center font-mono text-xs text-slate-500">
              Paste a token CA above and click Run Scan to inspect pre-breakout buyers.
            </div>
          ) : (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-800 bg-slate-950/70 px-3.5 py-2.5 font-mono text-xs">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-bold text-emerald-300">${result.symbol}</span>
                  <span className="text-slate-400">
                    Pool: <span className="text-slate-200">{truncateAddress(result.pool_address)}</span>
                  </span>
                  <span className="text-slate-400">
                    Breakout: <span className="text-cyan-300">{formatShortTime(result.breakout_utc)}</span>
                  </span>
                  <span className="text-slate-400">
                    Candidates: <span className="text-slate-100">{result.candidates.length}</span> (pages:{" "}
                    {result.pages_scanned}/5)
                  </span>
                </div>

                {newWallets.length > 0 && (
                  <button
                    type="button"
                    onClick={() => copyText(newWallets.join("\n"), "copy-all-new")}
                    className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-emerald-500/50 bg-emerald-500/15 px-2.5 py-1 font-mono text-[11px] font-semibold text-emerald-300 hover:bg-emerald-500/25"
                  >
                    {copiedKey === "copy-all-new" ? (
                      <>
                        <Check className="h-3.5 w-3.5" />
                        Copied {newWallets.length} New!
                      </>
                    ) : (
                      <>
                        <Copy className="h-3.5 w-3.5" />
                        Copy All New Addresses ({newWallets.length})
                      </>
                    )}
                  </button>
                )}
              </div>

              <div className="overflow-x-auto rounded-xl border border-slate-800/80">
                <table className="w-full border-collapse text-left font-mono text-xs">
                  <thead className="bg-slate-950 text-[10px] uppercase tracking-wider text-slate-400">
                    <tr>
                      <th className="px-3.5 py-2.5">Wallet</th>
                      <th className="px-3.5 py-2.5">SOL Spent</th>
                      <th className="px-3.5 py-2.5">Timing</th>
                      <th className="px-3.5 py-2.5">Buys</th>
                      <th className="px-3.5 py-2.5">Status</th>
                      <th className="px-3.5 py-2.5 text-right">Vet Links</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60">
                    {result.candidates.length === 0 ? (
                      <tr>
                        <td colSpan={6} className="px-4 py-8 text-center text-slate-500">
                          No qualifying buyers (&ge; {result.min_sol} SOL) found in the {result.lookback_mins}m pre-breakout window.
                        </td>
                      </tr>
                    ) : (
                      result.candidates.map((c) => {
                        const isBefore = c.mins_before_breakout >= 0;
                        return (
                          <tr key={c.wallet} className="hover:bg-slate-800/35">
                            <td className="px-3.5 py-2.5">
                              <div className="flex items-center gap-2">
                                <span className="font-semibold text-slate-100">
                                  {truncateAddress(c.wallet)}
                                </span>
                                <button
                                  type="button"
                                  onClick={() => copyText(c.wallet, c.wallet)}
                                  title="Copy wallet address"
                                  className="cursor-pointer text-slate-400 hover:text-cyan-300"
                                >
                                  {copiedKey === c.wallet ? (
                                    <Check className="h-3.5 w-3.5 text-emerald-400" />
                                  ) : (
                                    <Copy className="h-3.5 w-3.5" />
                                  )}
                                </button>
                              </div>
                            </td>
                            <td className="px-3.5 py-2.5 font-semibold tabular-nums text-emerald-300">
                              {formatSol(c.total_sol_spent)}
                            </td>
                            <td className="px-3.5 py-2.5 tabular-nums">
                              <span className={isBefore ? "text-cyan-300" : "text-amber-300"}>
                                {Math.abs(c.mins_before_breakout).toFixed(1)}m{" "}
                                {isBefore ? "before" : "after"}
                              </span>
                            </td>
                            <td className="px-3.5 py-2.5 tabular-nums text-slate-200">
                              {c.buy_count}
                            </td>
                            <td className="px-3.5 py-2.5">
                              {c.already_in_roster ? (
                                <span className="rounded-md border border-cyan-500/40 bg-cyan-500/15 px-2 py-0.5 text-[10px] font-semibold text-cyan-300">
                                  IN ROSTER
                                </span>
                              ) : (
                                <span className="rounded-md border border-fuchsia-500/40 bg-fuchsia-500/15 px-2 py-0.5 text-[10px] font-semibold text-fuchsia-300">
                                  NEW
                                </span>
                              )}
                            </td>
                            <td className="px-3.5 py-2.5 text-right">
                              <div className="inline-flex items-center gap-2.5">
                                <a
                                  href={c.gmgn_url}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="inline-flex items-center gap-0.5 text-[11px] text-emerald-400 hover:text-emerald-300"
                                >
                                  GMGN <ExternalLink className="h-2.5 w-2.5" />
                                </a>
                                <a
                                  href={c.solscan_url}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="inline-flex items-center gap-0.5 text-[11px] text-sky-400 hover:text-sky-300"
                                >
                                  Solscan <ExternalLink className="h-2.5 w-2.5" />
                                </a>
                              </div>
                            </td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
