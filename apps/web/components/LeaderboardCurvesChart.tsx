"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { LeaderboardCurvesResponse } from "@/lib/api";
import { CHART_PALETTE } from "@/lib/palette";

type Props = {
  data: LeaderboardCurvesResponse;
};

const PALETTE = CHART_PALETTE;

function fmtPct(v: number | null | undefined, digits = 1) {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const s = (v * 100).toFixed(digits);
  const n = Number(s);
  return `${n > 0 ? "+" : ""}${s}%`;
}

function fmtDate(t: number) {
  return new Date(t).toISOString().slice(0, 10);
}

// Round bounds outward to a nice 5% / 10% / 25% step depending on range.
function pickStep(range: number): number {
  if (range <= 0.30) return 0.05;
  if (range <= 0.80) return 0.10;
  if (range <= 2.00) return 0.25;
  if (range <= 5.00) return 0.50;
  return 1.00;
}

function buildYTicks(yMin: number, yMax: number): {
  ticks: number[];
  domain: [number, number];
} {
  // Make sure 0 is always inside the domain.
  yMin = Math.min(0, yMin);
  yMax = Math.max(0, yMax);
  const range = yMax - yMin;
  if (range === 0) {
    return { ticks: [-0.05, 0, 0.05], domain: [-0.05, 0.05] };
  }
  const step = pickStep(range);
  const lo = Math.floor(yMin / step) * step;
  const hi = Math.ceil(yMax / step) * step;
  const ticks: number[] = [];
  // Float-math safe range loop
  const n = Math.round((hi - lo) / step);
  for (let i = 0; i <= n; i++) {
    ticks.push(Number((lo + i * step).toFixed(6)));
  }
  return { ticks, domain: [lo, hi] };
}

export function LeaderboardCurvesChart({ data }: Props) {
  const [pinned, setPinned] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const accounts = data.accounts;

  const { rows, lines, yTicks, yDomain, xDomain, xTicks } = useMemo(() => {
    if (accounts.length === 0) {
      const now = Date.now();
      return {
        rows: [],
        lines: [] as { handle: string; color: string; final: number; n: number; median: number | null; damped: number }[],
        yTicks: [-0.05, 0, 0.05],
        yDomain: [-0.05, 0.05] as [number, number],
        xDomain: [now - 86_400_000, now] as [number, number],
        xTicks: [] as number[],
      };
    }
    const today = Date.now();
    // Build a per-account map: ts -> cum_mean at each real event, plus a
    // synthetic "today" anchor at the account's last value so the curve
    // visibly extends to today.
    const eventsByHandle = new Map<string, Map<number, number>>();
    for (const a of accounts) {
      const m = new Map<number, number>();
      for (const p of a.curve) m.set(new Date(p.ts).getTime(), p.cum_mean);
      const last = a.curve[a.curve.length - 1]?.cum_mean;
      if (last !== undefined && !m.has(today)) m.set(today, last);
      eventsByHandle.set(a.handle, m);
    }

    // Union of all timestamps where ANY account has a real or synthetic point.
    const tsSet = new Set<number>();
    eventsByHandle.forEach((m) => m.forEach((_, ts) => tsSet.add(ts)));
    const tsList = [...tsSet].sort((a, b) => a - b);

    // Emit values ONLY at an account's own event timestamps; null elsewhere.
    // Combined with connectNulls + type="monotone" this gives a smooth spline
    // through the real points instead of a forward-filled staircase.
    const rows = tsList.map((ts) => {
      const row: Record<string, number | null> = { ts };
      for (const a of accounts) {
        const v = eventsByHandle.get(a.handle)?.get(ts);
        row[a.handle] = v === undefined ? null : v;
      }
      return row;
    });

    const earliest = tsList[0];
    const xMin = earliest;
    const xMax = today;

    // Y range across all points (excluding nulls)
    const allY: number[] = [];
    for (const r of rows) {
      for (const a of accounts) {
        const v = r[a.handle];
        if (typeof v === "number" && Number.isFinite(v)) allY.push(v);
      }
    }
    let yMin = allY.length ? Math.min(...allY) : -0.05;
    let yMax = allY.length ? Math.max(...allY) : 0.05;
    const { ticks: yTicks, domain: yDomain } = buildYTicks(yMin, yMax);

    // 6 x-ticks evenly spaced
    const tickCount = 6;
    const xTicks: number[] = [];
    for (let i = 0; i <= tickCount; i++) {
      xTicks.push(Math.round(xMin + ((xMax - xMin) * i) / tickCount));
    }

    const lines = accounts.map((a, i) => {
      const final = a.curve[a.curve.length - 1]?.cum_mean ?? 0;
      const n = a.n_matured;
      const median = a.median_excess;
      // damped = median * sqrt(n / (n + 5))
      const damped =
        median !== null && Number.isFinite(median) && n > 0
          ? median * Math.sqrt(n / (n + 5))
          : 0;
      return {
        handle: a.handle,
        color: PALETTE[i % PALETTE.length],
        final,
        n,
        median,
        damped,
      };
    });

    return { rows, lines, yTicks, yDomain, xDomain: [xMin, xMax] as [number, number], xTicks };
  }, [accounts]);

  if (accounts.length === 0) {
    return (
      <div className="rounded-lg border border-white/10 bg-surface p-6 text-sm text-muted">
        No equity-curve data for this cohort yet.
      </div>
    );
  }

  const activeHandle = pinned ?? hovered;

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm uppercase tracking-wider text-muted">
          how the leaderboard is built
        </h2>
        <p className="text-xs text-muted">
          running mean BTC-excess per account · x = calendar time · curve ends today
        </p>
      </div>
      <div className="relative rounded-lg border border-white/10 bg-surface p-3">
        <div className="h-[380px] w-full">
          {mounted ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={rows}
              margin={{ top: 12, right: 24, left: 4, bottom: 8 }}
              onClick={() => setPinned(null)}
            >
              <CartesianGrid stroke="rgba(255,255,255,0.06)" strokeDasharray="3 3" />
              <XAxis
                dataKey="ts"
                type="number"
                domain={xDomain}
                ticks={xTicks}
                tickFormatter={(t) => fmtDate(t as number)}
                stroke="rgba(255,255,255,0.25)"
                tick={{ fill: "#9aa0a6", fontSize: 11 }}
                tickLine={{ stroke: "rgba(255,255,255,0.2)" }}
                axisLine={{ stroke: "rgba(255,255,255,0.15)" }}
              />
              <YAxis
                type="number"
                domain={yDomain}
                ticks={yTicks}
                tickFormatter={(v) => fmtPct(v as number, 0)}
                stroke="rgba(255,255,255,0.25)"
                tick={{ fill: "#9aa0a6", fontSize: 11 }}
                tickLine={{ stroke: "rgba(255,255,255,0.2)" }}
                axisLine={{ stroke: "rgba(255,255,255,0.15)" }}
                width={56}
              />
              <ReferenceLine y={0} stroke="rgba(255,255,255,0.25)" strokeWidth={1} />
              <Tooltip
                shared
                trigger="hover"
                content={(p) => (
                  <TooltipContent
                    payload={p.payload}
                    label={p.label}
                    active={p.active}
                    lines={lines}
                    activeHandle={activeHandle}
                  />
                )}
                cursor={{ stroke: "rgba(255,255,255,0.25)", strokeDasharray: "2 4" }}
                wrapperStyle={{ outline: "none" }}
                isAnimationActive={false}
              />
              {lines.map((l) => {
                const isActive = activeHandle === l.handle;
                const anyActive = activeHandle !== null;
                const opacity = !anyActive ? 0.95 : isActive ? 1 : 0.15;
                const width = isActive ? 2.6 : 1.6;
                return (
                  <Line
                    key={l.handle}
                    type="monotone"
                    dataKey={l.handle}
                    stroke={l.color}
                    strokeWidth={width}
                    strokeOpacity={opacity}
                    // Visible dots at every real data point so each line is
                    // anchored — without these the bare curves are impossible
                    // to attribute to an account.
                    dot={{
                      r: isActive ? 3.5 : 2.5,
                      fill: l.color,
                      stroke: "rgba(0,0,0,0.6)",
                      strokeWidth: 0.5,
                      fillOpacity: opacity,
                    }}
                    activeDot={{
                      r: 5,
                      fill: l.color,
                      stroke: "rgba(255,255,255,0.6)",
                      strokeWidth: 1,
                    }}
                    isAnimationActive={false}
                    connectNulls={true}
                  />
                );
              })}
            </LineChart>
          </ResponsiveContainer>
          ) : null}
        </div>
        {pinned ? (
          <div className="absolute right-4 top-4 rounded-md border border-white/10 bg-bg/95 px-2 py-1 text-[11px] text-muted shadow-md backdrop-blur">
            pinned: @{pinned} · click chart to unpin
          </div>
        ) : null}
      </div>

      {/* Legend / score-comparison strip */}
      <div className="overflow-x-auto rounded-lg border border-white/10 bg-surface">
        <table className="w-full text-xs">
          <thead className="bg-white/[0.03] text-[10px] uppercase tracking-wider text-muted">
            <tr>
              <th className="px-2 py-1.5 text-left">acct</th>
              <th className="px-2 py-1.5 text-right">n</th>
              <th className="px-2 py-1.5 text-right" title="running raw mean — the line you see">
                mean (line)
              </th>
              <th className="px-2 py-1.5 text-right" title="median excess — used as the table sort key">
                median
              </th>
              <th
                className="px-2 py-1.5 text-right"
                title="median × √(N / (N + 5)) — the dampened leaderboard rank score"
              >
                dampened
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.06]">
            {lines.map((l) => {
              const isActive = activeHandle === l.handle;
              return (
                <tr
                  key={l.handle}
                  onMouseEnter={() => setHovered(l.handle)}
                  onMouseLeave={() => setHovered(null)}
                  onClick={() =>
                    setPinned((p) => (p === l.handle ? null : l.handle))
                  }
                  className={`cursor-pointer ${isActive ? "bg-white/[0.04]" : "hover:bg-white/[0.02]"}`}
                >
                  <td className="px-2 py-1.5">
                    <span className="inline-flex items-center gap-1.5">
                      <span
                        className="inline-block h-2.5 w-2.5 rounded-full"
                        style={{ background: l.color }}
                      />
                      <Link
                        href={`/account/${l.handle}`}
                        className="text-ink hover:text-accent hover:underline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        @{l.handle}
                      </Link>
                    </span>
                  </td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-muted">{l.n}</td>
                  <td
                    className={`px-2 py-1.5 text-right tabular-nums ${
                      l.final > 0
                        ? "text-emerald-400"
                        : l.final < 0
                          ? "text-rose-400"
                          : "text-muted"
                    }`}
                  >
                    {fmtPct(l.final)}
                  </td>
                  <td
                    className={`px-2 py-1.5 text-right tabular-nums ${
                      (l.median ?? 0) > 0
                        ? "text-emerald-400"
                        : (l.median ?? 0) < 0
                          ? "text-rose-400"
                          : "text-muted"
                    }`}
                  >
                    {fmtPct(l.median)}
                  </td>
                  <td
                    className={`px-2 py-1.5 text-right tabular-nums ${
                      l.damped > 0
                        ? "text-emerald-400"
                        : l.damped < 0
                          ? "text-rose-400"
                          : "text-muted"
                    }`}
                  >
                    {fmtPct(l.damped)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="text-[10px] text-muted">
        Why three numbers? The chart plots the running <span className="text-ink">raw mean</span>{" "}
        — easy to read off as a curve but volatile. The leaderboard sorts by{" "}
        <span className="text-ink">dampened</span> = median × √(N / (N + 5)), which trims tails and
        penalizes thin samples. Hover or click a row to isolate that account.
      </p>
    </div>
  );
}

function TooltipContent({
  payload,
  label,
  active,
  lines,
  activeHandle,
}: {
  // recharts' TooltipPayload type uses a wider dataKey union; we narrow at use site
  payload: readonly { dataKey?: unknown; value?: unknown; color?: string }[] | undefined;
  label: string | number | undefined;
  active: boolean | undefined;
  lines: { handle: string; color: string }[];
  activeHandle: string | null;
}) {
  if (!active || !payload || payload.length === 0) return null;
  // Sort by value descending; if a row is "pinned/hovered" pull it up.
  const byHandle = new Map(payload.map((p) => [p.dataKey as string, p]));
  const rows = lines
    .map((l) => {
      const p = byHandle.get(l.handle);
      const v = p?.value;
      return { handle: l.handle, color: l.color, v: typeof v === "number" ? v : null };
    })
    .filter((r) => r.v !== null);
  rows.sort((a, b) => {
    if (a.handle === activeHandle) return -1;
    if (b.handle === activeHandle) return 1;
    return (b.v as number) - (a.v as number);
  });

  return (
    <div className="rounded-md border border-white/10 bg-bg/95 px-3 py-2 text-xs shadow-lg backdrop-blur">
      <div className="mb-1 text-muted">{fmtDate(label as number)}</div>
      <ul className="space-y-0.5 tabular-nums">
        {rows.slice(0, 12).map((r) => (
          <li key={r.handle} className="flex items-center gap-2">
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ background: r.color }}
            />
            <span
              className={`flex-1 truncate ${r.handle === activeHandle ? "text-accent" : "text-ink"}`}
            >
              @{r.handle}
            </span>
            <span
              className={
                (r.v as number) > 0
                  ? "text-emerald-400"
                  : (r.v as number) < 0
                    ? "text-rose-400"
                    : "text-muted"
              }
            >
              {fmtPct(r.v)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
