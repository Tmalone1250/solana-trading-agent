"use client";

import { useState } from "react";
import { Check, Copy, ExternalLink, Radar, RefreshCw } from "lucide-react";
import {
  WhaleDiscoveryPayload,
  formatRelativeTime,
  formatSol,
  truncateAddress,
} from "@/lib/types";

interface Props {
  whale: WhaleDiscoveryPayload | undefined;
  agentUrl: string;
  onSynced?: () => void;
}

type TabKey = "confluence" | "tape" | "roster" | "trades";

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
  if (r === "WHALE_PEAK_TRAIL") return "border-cyan-500/40 bg-cyan-500/15 text-cyan-300";
  if (r === "WHALE_SHADOW_EXIT") return "border-amber-500/50 bg-amber-500/20 text-amber-300";
  if (r === "WHALE_HARD_STOP") return "border-rose-500/50 bg-rose-500/20 text-rose-300";
  if (r === "WHALE_TIME_STOP") return "border-slate-600 bg-slate-800/80 text-slate-300";
  return "border-slate-700 bg-slate-900 text-slate-300";
}

export function WhaleListCard({ whale, agentUrl, onSynced }: Props) {
  const [tab, setTab] = useState<TabKey>("confluence");
  const [syncing, setSyncing] = useState(false);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const enabled = whale?.enabled ?? false;
  const trackedCount = whale?.tracked_wallet_count ?? 0;
  const eventsToday = whale?.events_today ?? 0;
  const activeMints = whale?.active_whale_mints ?? 0;
  const solVolumeToday = whale?.total_sol_volume_today ?? 0;
  const signals = whale?.active_signals ?? [];
  const events = whale?.recent_events ?? [];
  const roster = whale?.roster ?? [];
  const whaleTrades = whale?.whale_trades ?? [];
  const whaleStats = whale?.whale_stats;

  async function handleSyncHelius() {
    if (syncing) return;
    setSyncing(true);
    setSyncMsg(null);
    try {
      const res = await fetch(`${agentUrl}/api/webhooks/helius/sync`, {
        method: "POST",
        cache: "no-store",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: { synced_count?: number } = await res.json();
      setSyncMsg(`Synced ${data.synced_count ?? 0} wallets`);
      onSynced?.();
    } catch (err) {
      setSyncMsg(`Sync failed: ${err instanceof Error ? err.message : "error"}`);
    } finally {
      setSyncing(false);
    }
  }

  function handleCopy(value: string) {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      navigator.clipboard.writeText(value).catch(() => {});
      setCopiedKey(value);
      setTimeout(() => setCopiedKey((prev) => (prev === value ? null : prev)), 1500);
    }
  }

  return (
    <section className="rounded-2xl border border-slate-800/80 bg-slate-900/60">
      {/* Header */}
      <div className="flex flex-col gap-3 border-b border-slate-800/80 px-5 py-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex flex-wrap items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-cyan-500/30 bg-cyan-500/10 text-cyan-400">
            <Radar className="h-4 w-4" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold text-slate-100">Whale Radar</h2>
              <span
                className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[10px] font-semibold ${
                  enabled
                    ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
                    : "border-slate-700 bg-slate-800/80 text-slate-400"
                }`}
              >
                <span
                  className={`h-1.5 w-1.5 rounded-full ${
                    enabled ? "animate-pulse bg-emerald-400" : "bg-slate-500"
                  }`}
                />
                {enabled ? "ARMED" : "OFF"}
              </span>
            </div>
            <p className="text-xs text-slate-400">
              Helius Enhanced Webhook smart-money confluence &amp; live wallet roster
            </p>
          </div>

          <button
            type="button"
            onClick={handleSyncHelius}
            disabled={syncing}
            className="inline-flex cursor-pointer items-center gap-1.5 rounded-full border border-slate-700/80 bg-slate-950/80 px-2.5 py-1 font-mono text-[11px] font-medium text-slate-200 transition hover:border-cyan-500/60 hover:text-cyan-300 disabled:cursor-wait disabled:opacity-60"
          >
            <RefreshCw className={`h-3 w-3 text-cyan-400 ${syncing ? "animate-spin" : ""}`} />
            {syncing ? "Syncing..." : "Sync Helius ↻"}
          </button>
          {syncMsg && (
            <span className="font-mono text-[11px] text-cyan-300">{syncMsg}</span>
          )}
        </div>

        {/* 4 Compact KPI Pills */}
        <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:items-center">
          <div className="rounded-lg border border-slate-800 bg-slate-950/70 px-2.5 py-1.5 font-mono">
            <div className="text-[10px] uppercase tracking-wider text-slate-500">Tracked Wallets</div>
            <div className="text-xs font-semibold tabular-nums text-slate-100">{trackedCount}</div>
          </div>
          <div className="rounded-lg border border-slate-800 bg-slate-950/70 px-2.5 py-1.5 font-mono">
            <div className="text-[10px] uppercase tracking-wider text-slate-500">Events Today</div>
            <div className="text-xs font-semibold tabular-nums text-slate-100">{eventsToday}</div>
          </div>
          <div className="rounded-lg border border-slate-800 bg-slate-950/70 px-2.5 py-1.5 font-mono">
            <div className="text-[10px] uppercase tracking-wider text-slate-500">Active Mints</div>
            <div className="text-xs font-semibold tabular-nums text-cyan-300">{activeMints}</div>
          </div>
          <div className="rounded-lg border border-slate-800 bg-slate-950/70 px-2.5 py-1.5 font-mono">
            <div className="text-[10px] uppercase tracking-wider text-slate-500">SOL Flow Today</div>
            <div className="text-xs font-semibold tabular-nums text-emerald-300">
              {formatSol(solVolumeToday)}
            </div>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-slate-800/80 bg-slate-950/40 px-4 pt-2 font-mono text-xs">
        {(
          [
            { key: "confluence", label: `Confluence (${signals.length})` },
            { key: "tape", label: `Live Tape (${events.length})` },
            { key: "roster", label: `Roster (${roster.length})` },
            { key: "trades", label: `Agent Trades (${whaleTrades.length})` },
          ] as Array<{ key: TabKey; label: string }>
        ).map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            className={`cursor-pointer border-b-2 px-3 py-2 text-[11px] font-medium transition ${
              tab === t.key
                ? "border-cyan-400 text-cyan-300"
                : "border-transparent text-slate-400 hover:text-slate-200"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab 1: Confluence */}
      {tab === "confluence" && (
        <div className="terminal-scroll max-h-[336px] overflow-x-auto overflow-y-auto">
          <table className="w-full border-collapse text-left text-xs">
            <thead className="sticky top-0 z-10 bg-slate-950 shadow-[0_1px_0_0_rgba(30,41,59,0.9)]">
              <tr className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
                <th className="px-4 py-2.5">Mint</th>
                <th className="px-4 py-2.5">Unique Whales</th>
                <th className="px-4 py-2.5">Buys / Sells</th>
                <th className="px-4 py-2.5">SOL Spent</th>
                <th className="px-4 py-2.5">Net Status</th>
                <th className="px-4 py-2.5">Agent</th>
                <th className="px-4 py-2.5 text-right">Last Seen</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60 font-mono">
              {signals.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-xs text-slate-500">
                    {eventsToday === 0
                      ? "No whale swaps received today — listening on POST /api/webhooks/helius."
                      : "No active whale confluence signals in the current TTL window."}
                  </td>
                </tr>
              ) : (
                signals.map((sig) => {
                  const accumulating = sig.buy_count >= sig.sell_count;
                  const multiWhale = sig.unique_whales >= 2;
                  const isEntered = sig.agent_status === "ENTERED";
                  return (
                    <tr key={sig.mint} className="hover:bg-slate-800/30">
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          <span className="font-semibold text-slate-100">
                            {truncateAddress(sig.mint)}
                          </span>
                          <button
                            type="button"
                            onClick={() => handleCopy(sig.mint)}
                            title="Copy mint"
                            className="cursor-pointer text-slate-500 hover:text-slate-200"
                          >
                            {copiedKey === sig.mint ? (
                              <Check className="h-3.5 w-3.5 text-emerald-400" />
                            ) : (
                              <Copy className="h-3.5 w-3.5" />
                            )}
                          </button>
                          <a
                            href={`https://dexscreener.com/solana/${sig.mint}`}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-0.5 text-[11px] text-sky-400 hover:text-sky-300"
                          >
                            Dex <ExternalLink className="h-2.5 w-2.5" />
                          </a>
                          <a
                            href={`https://solscan.io/token/${sig.mint}`}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-0.5 text-[11px] text-slate-400 hover:text-slate-200"
                          >
                            Solscan <ExternalLink className="h-2.5 w-2.5" />
                          </a>
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex rounded-md border px-2 py-0.5 text-[11px] font-semibold ${
                            multiWhale
                              ? "border-amber-500/50 bg-amber-500/20 text-amber-300"
                              : "border-slate-700 bg-slate-800/80 text-slate-300"
                          }`}
                        >
                          {sig.unique_whales} {sig.unique_whales === 1 ? "whale" : "whales"}
                        </span>
                      </td>
                      <td className="px-4 py-3 tabular-nums">
                        <span className="text-emerald-400">{sig.buy_count}B</span>
                        <span className="text-slate-600"> / </span>
                        <span className="text-rose-400">{sig.sell_count}S</span>
                      </td>
                      <td className="px-4 py-3 font-semibold tabular-nums text-slate-100">
                        {formatSol(sig.total_sol_spent)}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex rounded-md border px-2 py-0.5 text-[10px] font-semibold ${
                            accumulating
                              ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
                              : "border-rose-500/40 bg-rose-500/15 text-rose-300"
                          }`}
                        >
                          {accumulating ? "ACCUMULATING" : "SELL PRESSURE"}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        {isEntered ? (
                          <span className="inline-flex rounded-md border border-emerald-500/50 bg-emerald-500/20 px-2 py-0.5 text-[10px] font-semibold text-emerald-300">
                            ENTERED
                          </span>
                        ) : sig.agent_reason ? (
                          <span className="inline-flex rounded-md border border-slate-700/80 bg-slate-900/90 px-2 py-0.5 text-[10px] text-slate-400">
                            {sig.agent_reason}
                          </span>
                        ) : (
                          <span className="text-[10px] text-slate-600">PENDING</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right text-slate-400">
                        {formatRelativeTime(sig.last_seen_utc)}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Tab 2: Live Tape */}
      {tab === "tape" && (
        <div className="terminal-scroll max-h-[336px] overflow-x-auto overflow-y-auto">
          <table className="w-full border-collapse text-left text-xs">
            <thead className="sticky top-0 z-10 bg-slate-950 shadow-[0_1px_0_0_rgba(30,41,59,0.9)]">
              <tr className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
                <th className="px-4 py-2.5">Time</th>
                <th className="px-4 py-2.5">Wallet</th>
                <th className="px-4 py-2.5">Side</th>
                <th className="px-4 py-2.5">SOL Size</th>
                <th className="px-4 py-2.5">Mint</th>
                <th className="px-4 py-2.5 text-right">TX</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60 font-mono">
              {events.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-xs text-slate-500">
                    No whale swaps recorded yet — events will stream here as Helius delivers SWAP payloads.
                  </td>
                </tr>
              ) : (
                events.map((ev) => {
                  const isBuy = ev.side.toUpperCase() === "BUY";
                  return (
                    <tr key={ev.signature} className="hover:bg-slate-800/30">
                      <td className="px-4 py-3 text-slate-400">
                        {formatRelativeTime(ev.timestamp_utc)}
                      </td>
                      <td className="px-4 py-3">
                        <a
                          href={`https://solscan.io/account/${ev.wallet}`}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 font-sans font-semibold text-slate-100 hover:text-cyan-300"
                        >
                          {ev.wallet_label}
                          <ExternalLink className="h-3 w-3 text-slate-500" />
                        </a>
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex rounded border px-2 py-0.5 text-[10px] font-semibold ${
                            isBuy
                              ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
                              : "border-rose-500/40 bg-rose-500/15 text-rose-300"
                          }`}
                        >
                          {ev.side.toUpperCase()}
                        </span>
                      </td>
                      <td className="px-4 py-3 tabular-nums text-slate-200">
                        {formatSol(ev.sol_amount)}
                      </td>
                      <td className="px-4 py-3">
                        <a
                          href={`https://dexscreener.com/solana/${ev.mint}`}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 text-sky-400 hover:text-sky-300"
                        >
                          {truncateAddress(ev.mint)}
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      </td>
                      <td className="px-4 py-3 text-right">
                        <a
                          href={`https://solscan.io/tx/${ev.signature}`}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-0.5 text-slate-400 hover:text-cyan-300"
                        >
                          TX ↗
                        </a>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Tab 3: Roster */}
      {tab === "roster" && (
        <div className="terminal-scroll max-h-[336px] overflow-x-auto overflow-y-auto">
          <table className="w-full border-collapse text-left text-xs">
            <thead className="sticky top-0 z-10 bg-slate-950 shadow-[0_1px_0_0_rgba(30,41,59,0.9)]">
              <tr className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
                <th className="px-4 py-2.5">Wallet</th>
                <th className="px-4 py-2.5">24h Buys / Sells</th>
                <th className="px-4 py-2.5">24h SOL Volume</th>
                <th className="px-4 py-2.5">Last Action</th>
                <th className="px-4 py-2.5 text-right">Last Active</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60 font-mono">
              {roster.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-4 py-8 text-center text-xs text-slate-500">
                    No tracked wallets synced yet — configure HELIUS_API_KEY and click Sync Helius ↻.
                  </td>
                </tr>
              ) : (
                roster.map((row) => (
                  <tr key={row.wallet} className="hover:bg-slate-800/30">
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <span className="font-sans font-semibold text-slate-100">
                          {row.wallet_label}
                        </span>
                        <span className="text-[11px] text-slate-500">
                          ({truncateAddress(row.wallet)})
                        </span>
                        <button
                          type="button"
                          onClick={() => handleCopy(row.wallet)}
                          title="Copy wallet address"
                          className="cursor-pointer text-slate-500 hover:text-slate-200"
                        >
                          {copiedKey === row.wallet ? (
                            <Check className="h-3.5 w-3.5 text-emerald-400" />
                          ) : (
                            <Copy className="h-3.5 w-3.5" />
                          )}
                        </button>
                        <a
                          href={`https://solscan.io/account/${row.wallet}`}
                          target="_blank"
                          rel="noreferrer"
                          className="text-slate-400 hover:text-cyan-300"
                          title="View on Solscan"
                        >
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      </div>
                    </td>
                    <td className="px-4 py-3 tabular-nums">
                      <span className="text-emerald-400">{row.buys_24h}B</span>
                      <span className="text-slate-600"> / </span>
                      <span className="text-rose-400">{row.sells_24h}S</span>
                    </td>
                    <td className="px-4 py-3 tabular-nums text-slate-200">
                      {formatSol(row.total_sol_volume)}
                    </td>
                    <td className="px-4 py-3">
                      {row.last_side && row.last_mint ? (
                        <div className="flex items-center gap-1.5">
                          <span
                            className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold ${
                              row.last_side === "BUY"
                                ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
                                : "border-rose-500/40 bg-rose-500/15 text-rose-300"
                            }`}
                          >
                            {row.last_side}
                          </span>
                          <a
                            href={`https://dexscreener.com/solana/${row.last_mint}`}
                            target="_blank"
                            rel="noreferrer"
                            className="text-sky-400 hover:text-sky-300"
                          >
                            {truncateAddress(row.last_mint)}
                          </a>
                        </div>
                      ) : (
                        <span className="text-slate-500">Idle (0 swaps)</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right text-slate-400">
                      {formatRelativeTime(row.last_seen_utc)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
      {/* Tab 4: Agent Trades (WHALE_SCALP) */}
      {tab === "trades" && (
        <div>
          {/* Compact KPI Mini-Bar */}
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
                className={`font-semibold tabular-nums ${
                  (whaleStats?.total_pnl_sol ?? 0) > 0
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
                      t.pnl_sol > 0
                        ? "text-emerald-400"
                        : t.pnl_sol < 0
                          ? "text-rose-400"
                          : "text-slate-300";
                    return (
                      <tr
                        key={`${t.mint}-${t.closed_at || t.opened_at || idx}`}
                        className={isOpen ? "bg-fuchsia-950/15 hover:bg-fuchsia-950/25" : "hover:bg-slate-800/30"}
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
      )}
    </section>
  );
}
