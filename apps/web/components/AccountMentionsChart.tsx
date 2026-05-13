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
import type { MentionCurvesResponse } from "@/lib/api";
import { CHART_PALETTE } from "@/lib/palette";

type Props = {
  data: MentionCurvesResponse;
};

const PALETTE = CHART_PALETTE;

function fmtPct(v: number | null | undefined, digits = 1) {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const s = (v * 100).toFixed(digits);
  const n = Number(s);
  return `${n > 0 ? "+" : ""}${s}%`;
}

function fmtDate(iso: string) {
  return new Date(iso).toISOString().slice(0, 10);
}

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
  yMin = Math.min(0, yMin);
  yMax = Math.max(0, yMax);
  const range = yMax - yMin;
  if (range === 0) return { ticks: [-0.05, 0, 0.05], domain: [-0.05, 0.05] };
  const step = pickStep(range);
  const lo = Math.floor(yMin / step) * step;
  const hi = Math.ceil(yMax / step) * step;
  const ticks: number[] = [];
  const n = Math.round((hi - lo) / step);
  for (let i = 0; i <= n; i++) ticks.push(Number((lo + i * step).toFixed(6)));
  return { ticks, domain: [lo, hi] };
}

type Mention = MentionCurvesResponse["mentions"][number];

type TokenAgg = {
  symbol: string;
  n: number;
  mentions: Mention[];
  meanFinal: number;
  // Aggregated trajectory: one point per integer day, mean across mentions that
  // have data at that day. Sparse mentions are simply skipped at days where
  // they have no sample.
  points: { day: number; mean_excess: number; n: number }[];
};

export function AccountMentionsChart({ data }: Props) {
  const [pinned, setPinned] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const { tokens, rows, yTicks, yDomain, xDomain, xTicks } = useMemo(() => {
    const horizon = data.horizon_days;
    if (data.mentions.length === 0) {
      return {
        tokens: [] as TokenAgg[],
        rows: [] as Record<string, number | null>[],
        yTicks: [-0.05, 0, 0.05],
        yDomain: [-0.05, 0.05] as [number, number],
        xDomain: [0, horizon] as [number, number],
        xTicks: [0],
      };
    }
    const grouped = new Map<string, Mention[]>();
    for (const m of data.mentions) {
      const sym = m.symbol ?? "?";
      const arr = grouped.get(sym) ?? [];
      arr.push(m);
      grouped.set(sym, arr);
    }

    // Build aggregated trajectory per token: for each integer day 0..horizon,
    // interpolate each mention's excess (from its discrete points) and mean
    // across mentions. Linear interp between adjacent samples.
    function interp(pts: { day: number; excess: number }[], d: number): number | null {
      if (pts.length === 0) return null;
      if (d < pts[0].day) return null;
      if (d > pts[pts.length - 1].day) return null;
      let lo = 0;
      for (let i = 1; i < pts.length; i++) {
        if (pts[i].day >= d) {
          lo = i - 1;
          break;
        }
        lo = i;
      }
      const a = pts[lo];
      const b = pts[Math.min(lo + 1, pts.length - 1)];
      if (a === b || b.day === a.day) return a.excess;
      const t = (d - a.day) / (b.day - a.day);
      return a.excess + t * (b.excess - a.excess);
    }

    const tokens: TokenAgg[] = [];
    for (const [symbol, mentions] of grouped.entries()) {
      const points: { day: number; mean_excess: number; n: number }[] = [];
      for (let d = 0; d <= horizon; d++) {
        const vals: number[] = [];
        for (const m of mentions) {
          const v = interp(m.points, d);
          if (v !== null && Number.isFinite(v)) vals.push(v);
        }
        if (vals.length > 0) {
          const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
          points.push({ day: d, mean_excess: mean, n: vals.length });
        }
      }
      const finals = mentions
        .map((m) => m.final_excess)
        .filter((v): v is number => v !== null && Number.isFinite(v));
      const meanFinal = finals.length
        ? finals.reduce((a, b) => a + b, 0) / finals.length
        : 0;
      tokens.push({
        symbol,
        n: mentions.length,
        mentions,
        meanFinal,
        points,
      });
    }
    // Sort by |meanFinal| descending so the top legend = biggest movers; ties broken by n.
    tokens.sort((a, b) => Math.abs(b.meanFinal) - Math.abs(a.meanFinal) || b.n - a.n);

    // Build wide rows for recharts: { day, SYM1, SYM2, ... }
    const xMax = horizon;
    const rows: Record<string, number | null>[] = [];
    for (let d = 0; d <= xMax; d++) {
      const row: Record<string, number | null> = { day: d };
      for (const t of tokens) {
        const p = t.points.find((pt) => pt.day === d);
        row[t.symbol] = p ? p.mean_excess : null;
      }
      rows.push(row);
    }

    const allY: number[] = [];
    for (const t of tokens) for (const p of t.points) allY.push(p.mean_excess);
    let yMin = allY.length ? Math.min(...allY) : -0.05;
    let yMax = allY.length ? Math.max(...allY) : 0.05;
    const { ticks: yTicks, domain: yDomain } = buildYTicks(yMin, yMax);

    const xTickValues =
      horizon === 30 ? [0, 5, 10, 15, 20, 25, 30] :
      horizon === 90 ? [0, 15, 30, 45, 60, 75, 90] :
      [0, 30, 60, 90, 180, 270, 365];

    return {
      tokens,
      rows,
      yTicks,
      yDomain,
      xDomain: [0, xMax] as [number, number],
      xTicks: xTickValues,
    };
  }, [data]);

  if (tokens.length === 0) {
    return (
      <div className="rounded-lg border border-white/10 bg-surface p-6 text-sm text-muted">
        Not enough matured calls with price coverage to plot for this cohort.
      </div>
    );
  }

  const tokenColor = (i: number) => PALETTE[i % PALETTE.length];
  const activeSym = pinned ?? hovered;
  const pinnedToken = pinned ? tokens.find((t) => t.symbol === pinned) ?? null : null;

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm uppercase tracking-wider text-muted">
          aggregate trajectory by token
        </h2>
        <p className="text-xs text-muted">
          one line per token · mean BTC-excess across all calls of that token, t0-anchored
        </p>
      </div>
      <div className="relative rounded-lg border border-white/10 bg-surface p-3">
        <div className="h-[400px] w-full">
          {mounted ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={rows}
              margin={{ top: 12, right: 24, left: 4, bottom: 8 }}
              onClick={() => setPinned(null)}
            >
              <CartesianGrid stroke="rgba(255,255,255,0.06)" strokeDasharray="3 3" />
              <XAxis
                dataKey="day"
                type="number"
                domain={xDomain}
                ticks={xTicks}
                tickFormatter={(v) => (v === 0 ? "t=0" : `${v}d`)}
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
                  <MentionTooltipContent
                    payload={p.payload}
                    label={p.label}
                    active={p.active}
                    tokens={tokens}
                    activeSym={activeSym}
                  />
                )}
                cursor={{ stroke: "rgba(255,255,255,0.25)", strokeDasharray: "2 4" }}
                wrapperStyle={{ outline: "none" }}
                isAnimationActive={false}
              />
              {tokens.map((t, i) => {
                const isActive = activeSym === t.symbol;
                const anyActive = activeSym !== null;
                const opacity = !anyActive ? 0.95 : isActive ? 1 : 0.15;
                const width = isActive ? 2.6 : 1.6;
                const color = tokenColor(i);
                return (
                  <Line
                    key={t.symbol}
                    type="monotone"
                    dataKey={t.symbol}
                    stroke={color}
                    strokeWidth={width}
                    strokeOpacity={opacity}
                    dot={{
                      r: isActive ? 3 : 2,
                      fill: color,
                      stroke: "rgba(0,0,0,0.6)",
                      strokeWidth: 0.5,
                      fillOpacity: opacity,
                    }}
                    activeDot={{
                      r: 5,
                      fill: color,
                      stroke: "rgba(255,255,255,0.6)",
                      strokeWidth: 1,
                    }}
                    isAnimationActive={false}
                    connectNulls={false}
                  />
                );
              })}
            </LineChart>
          </ResponsiveContainer>
          ) : null}
        </div>
        {pinned ? (
          <div className="absolute right-4 top-4 rounded-md border border-white/10 bg-bg/95 px-2 py-1 text-[11px] text-muted shadow-md backdrop-blur">
            pinned: {pinned} · click chart to unpin
          </div>
        ) : null}
      </div>

      {/* Legend: token chips */}
      <div className="flex flex-wrap gap-x-2 gap-y-1.5 text-xs">
        {tokens.map((t, i) => {
          const isActive = activeSym === t.symbol;
          return (
            <button
              key={t.symbol}
              type="button"
              onMouseEnter={() => setHovered(t.symbol)}
              onMouseLeave={() => setHovered(null)}
              onClick={(e) => {
                e.stopPropagation();
                setPinned((p) => (p === t.symbol ? null : t.symbol));
              }}
              className={`flex items-center gap-1.5 rounded px-1.5 py-0.5 transition-colors ${
                isActive ? "bg-white/[0.08]" : "hover:bg-white/[0.04]"
              }`}
            >
              <span
                className="inline-block h-2.5 w-2.5 rounded-full"
                style={{ background: tokenColor(i) }}
              />
              <span className="text-ink font-medium">{t.symbol}</span>
              <span
                className={
                  t.meanFinal > 0
                    ? "text-emerald-400 tabular-nums"
                    : t.meanFinal < 0
                      ? "text-rose-400 tabular-nums"
                      : "text-muted tabular-nums"
                }
              >
                {fmtPct(t.meanFinal)}
              </span>
              <span className="text-muted">· n={t.n}</span>
            </button>
          );
        })}
      </div>

      {/* Pinned token detail panel */}
      {pinnedToken ? (
        <div className="rounded-lg border border-white/10 bg-surface">
          <div className="flex items-baseline justify-between gap-2 border-b border-white/[0.06] px-3 py-2">
            <div className="text-xs">
              <span className="text-muted">individual calls for </span>
              <span className="font-medium text-ink">{pinnedToken.symbol}</span>
              <span className="text-muted"> · n={pinnedToken.n} · mean </span>
              <span
                className={
                  pinnedToken.meanFinal > 0
                    ? "text-emerald-400 tabular-nums"
                    : pinnedToken.meanFinal < 0
                      ? "text-rose-400 tabular-nums"
                      : "text-muted tabular-nums"
                }
              >
                {fmtPct(pinnedToken.meanFinal)}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setPinned(null)}
              className="text-[11px] text-muted hover:text-ink"
            >
              close
            </button>
          </div>
          <ul className="divide-y divide-white/[0.06] text-xs">
            {[...pinnedToken.mentions]
              .sort((a, b) => new Date(b.tweet_ts).getTime() - new Date(a.tweet_ts).getTime())
              .map((m) => (
                <li
                  key={m.id}
                  className="flex items-center justify-between gap-3 px-3 py-1.5 hover:bg-white/[0.02]"
                >
                  <span className="text-muted tabular-nums">{fmtDate(m.tweet_ts)}</span>
                  <span
                    className={`flex-1 text-right tabular-nums ${
                      (m.final_excess ?? 0) > 0
                        ? "text-emerald-400"
                        : (m.final_excess ?? 0) < 0
                          ? "text-rose-400"
                          : "text-muted"
                    }`}
                  >
                    {fmtPct(m.final_excess)}
                  </span>
                  <Link
                    href={`/mention/${m.id}`}
                    className="text-accent hover:underline"
                  >
                    open →
                  </Link>
                </li>
              ))}
          </ul>
        </div>
      ) : null}

      <p className="text-[10px] text-muted">
        Click a token to see its individual calls. Each line is the mean of all that token's
        mentions at each day-from-tweet (linearly interpolated where samples don't line up).
      </p>
    </div>
  );
}

function MentionTooltipContent({
  payload,
  label,
  active,
  tokens,
  activeSym,
}: {
  payload: readonly { dataKey?: unknown; value?: unknown; color?: string }[] | undefined;
  label: string | number | undefined;
  active: boolean | undefined;
  tokens: TokenAgg[];
  activeSym: string | null;
}) {
  if (!active || !payload || payload.length === 0) return null;
  const byKey = new Map(payload.map((p) => [p.dataKey as string, p]));
  const rows = tokens
    .map((t, i) => {
      const p = byKey.get(t.symbol);
      const v = p?.value;
      const samplePt = t.points.find((pp) => pp.day === Number(label));
      return {
        symbol: t.symbol,
        color: PALETTE[i % PALETTE.length],
        v: typeof v === "number" ? v : null,
        n: samplePt?.n ?? 0,
      };
    })
    .filter((r) => r.v !== null);
  rows.sort((a, b) => {
    if (a.symbol === activeSym) return -1;
    if (b.symbol === activeSym) return 1;
    return Math.abs(b.v as number) - Math.abs(a.v as number);
  });
  return (
    <div className="rounded-md border border-white/10 bg-bg/95 px-3 py-2 text-xs shadow-lg backdrop-blur">
      <div className="mb-1 text-muted">day {label}</div>
      <ul className="space-y-0.5 tabular-nums">
        {rows.slice(0, 10).map((r) => (
          <li key={r.symbol} className="flex items-center gap-2">
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ background: r.color }}
            />
            <span
              className={`flex-1 truncate ${r.symbol === activeSym ? "text-accent" : "text-ink"}`}
            >
              {r.symbol}
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
            {r.n > 1 ? <span className="text-muted">·n={r.n}</span> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
