"use client";

import { BreakoutTelemetry } from "@/lib/types";

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

export function BreakoutMeter({ bt }: { bt?: BreakoutTelemetry | null }) {
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
  const tooltip = bt.failed_gates?.length
    ? `Failed: ${bt.failed_gates.join(" • ")}`
    : "All breakout gates passed";

  return (
    <div className="w-56" title={tooltip}>
      <div className="flex items-center justify-between gap-1 font-mono text-[10px]">
        <span className={`font-semibold ${st.text}`}>{bt.score}/100</span>
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
