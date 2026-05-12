"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import type { TokenChartsResponse, TokenChartsToken } from "@/lib/api";
import { CHART_PALETTE } from "@/lib/palette";

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const s = (v * 100).toFixed(digits);
  const n = Number(s);
  return `${n > 0 ? "+" : ""}${s}%`;
}

function fmtIndexed(v: number): string {
  return `${(v * 100).toFixed(0)}`;
}

function fmtDate(iso: string): string {
  return new Date(iso).toISOString().slice(0, 10);
}

type Props = { data: TokenChartsResponse };

export function TokenChartsGrid({ data }: Props) {
  const [hovered, setHovered] = useState<string | null>(null);

  const handleToColor = useMemo(() => {
    const m = new Map<string, string>();
    data.accounts.forEach((a, i) =>
      m.set(a.handle, CHART_PALETTE[i % CHART_PALETTE.length]),
    );
    return m;
  }, [data.accounts]);

  if (data.tokens.length === 0) {
    return (
      <div className="rounded-lg border border-white/10 bg-surface p-6 text-sm text-muted">
        No closed-window winners yet for the {data.cohort} cohort.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm uppercase tracking-wider text-muted">
          who caught the {data.cohort} winners
        </h2>
        <p className="text-xs text-muted">
          top {data.tokens.length} tokens by return over {data.horizon_days}d ·
          dots = top-{data.accounts.length} accounts&apos; mentions
        </p>
      </div>

      <p className="text-[11px] text-muted">
        <span className="text-amber-300">Survivor-biased by design.</span>{" "}
        Each panel was selected <em>because</em> the token ended the window up
        — so any dot looks good. The interesting signal is <em>which</em>{" "}
        accounts got there first vs late on each call, and how often the same
        accounts recur across panels. Skill lives in the leaderboard table
        below; this view is about coverage and timing.
      </p>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {data.tokens.map((t) => (
          <TokenPanel
            key={t.token_id}
            token={t}
            horizon={data.horizon_days}
            handleToColor={handleToColor}
            hovered={hovered}
          />
        ))}
      </div>

      {/* Legend strip — top accounts coloured */}
      <div className="overflow-x-auto rounded-lg border border-white/10 bg-surface">
        <table className="w-full text-xs">
          <thead className="bg-white/[0.03] text-[10px] uppercase tracking-wider text-muted">
            <tr>
              <th className="px-2 py-1.5 text-left">acct</th>
              <th className="px-2 py-1.5 text-right">leaderboard n</th>
              <th
                className="px-2 py-1.5 text-right"
                title="median BTC-excess return — the leaderboard sort key"
              >
                median excess
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.06]">
            {data.accounts.map((a) => {
              const color = handleToColor.get(a.handle) ?? "#9aa0a6";
              const isActive = hovered === a.handle;
              return (
                <tr
                  key={a.handle}
                  onMouseEnter={() => setHovered(a.handle)}
                  onMouseLeave={() => setHovered(null)}
                  className={`cursor-default ${isActive ? "bg-white/[0.04]" : "hover:bg-white/[0.02]"}`}
                >
                  <td className="px-2 py-1.5">
                    <span className="inline-flex items-center gap-1.5">
                      <span
                        className="inline-block h-2.5 w-2.5 rounded-full"
                        style={{ background: color }}
                      />
                      <Link
                        href={`/account/${a.handle}`}
                        className="text-ink hover:text-accent hover:underline"
                      >
                        @{a.handle}
                      </Link>
                    </span>
                  </td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-muted">
                    {a.n_closed}
                  </td>
                  <td
                    className={`px-2 py-1.5 text-right tabular-nums ${
                      (a.median_excess ?? 0) > 0
                        ? "text-emerald-400"
                        : (a.median_excess ?? 0) < 0
                          ? "text-rose-400"
                          : "text-muted"
                    }`}
                  >
                    {fmtPct(a.median_excess)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Custom-SVG TokenPanel.
//
// Why hand-rolled (no Recharts) here:
// - These panels are tiny (≈250px wide). Recharts' ComposedChart mixes Line
//   and Scatter activation, and its hit-testing in small areas snaps to whatever
//   element is closest — often a single scatter dot — so the tooltip "sticks"
//   to one day no matter where you hover. With only one mention (e.g. Venice
//   on VVV) the tooltip would never move off that dot.
// - Custom SVG + an overlay onMouseMove gives us a vertical cursor that snaps
//   to nearest integer day reliably, regardless of how sparse the data is.
// ---------------------------------------------------------------------------
function TokenPanel({
  token,
  horizon,
  handleToColor,
  hovered,
}: {
  token: TokenChartsToken;
  horizon: number;
  handleToColor: Map<string, string>;
  hovered: string | null;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(280);
  const [hoverDay, setHoverDay] = useState<number | null>(null);
  const HEIGHT = 200;

  useEffect(() => {
    if (!wrapRef.current) return;
    const el = wrapRef.current;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  // Inner plot geometry
  const pad = { top: 8, right: 10, bottom: 22, left: 36 };
  const innerW = Math.max(40, width - pad.left - pad.right);
  const innerH = HEIGHT - pad.top - pad.bottom;

  // Build per-day price map (key = rounded day) and per-handle scatter data.
  const { dayValues, byHandle, yMin, yMax, lineDays } = useMemo(() => {
    const dv = new Map<number, number>();
    for (const p of token.series) {
      const d = Math.round(p.day);
      if (d < 0 || d > horizon) continue;
      // Keep first occurrence — series should already be ordered
      if (!dv.has(d)) dv.set(d, p.indexed);
    }
    const bh = new Map<
      string,
      { day: number; indexed: number; captured_ret: number | null; tweet_ts: string }[]
    >();
    for (const m of token.mentions) {
      if (m.indexed === null) continue;
      const arr = bh.get(m.handle) ?? [];
      arr.push({
        day: m.day,
        indexed: m.indexed,
        captured_ret: m.captured_ret,
        tweet_ts: m.tweet_ts,
      });
      bh.set(m.handle, arr);
    }
    const allY: number[] = [...dv.values()];
    for (const arr of bh.values()) for (const p of arr) allY.push(p.indexed);
    const yMn = allY.length ? Math.min(...allY) : 0;
    const yMx = allY.length ? Math.max(...allY) : 1;
    const padY = (yMx - yMn) * 0.1 || 0.05;
    const ldays = [...dv.keys()].sort((a, b) => a - b);
    return {
      dayValues: dv,
      byHandle: bh,
      yMin: Math.max(0, yMn - padY),
      yMax: yMx + padY,
      lineDays: ldays,
    };
  }, [token.series, token.mentions, horizon]);

  const xScale = (day: number) =>
    pad.left + (day / Math.max(horizon, 1)) * innerW;
  const yScale = (v: number) =>
    pad.top + ((yMax - v) / Math.max(yMax - yMin, 1e-9)) * innerH;

  // Line path through real samples (only points where price data exists).
  const linePath = useMemo(() => {
    if (lineDays.length === 0) return "";
    return lineDays
      .map((d, i) => {
        const x = xScale(d);
        const y = yScale(dayValues.get(d) as number);
        return `${i === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
      })
      .join(" ");
  }, [lineDays, dayValues, width, yMin, yMax]); // eslint-disable-line react-hooks/exhaustive-deps

  // X tick positions
  const xTicks =
    horizon === 30 ? [0, 7, 14, 21, 30] : [0, 15, 30, 45, 60, 75, 90];
  // Y ticks — 3 lines: yMin, mid, yMax
  const yTicks = [yMin, (yMin + yMax) / 2, yMax];

  // Sort scatter handles so dimmed ones render below active.
  const handlesSorted = [...byHandle.keys()].sort((a, b) => {
    if (a === hovered) return 1;
    if (b === hovered) return -1;
    return 0;
  });

  function onMove(e: React.MouseEvent<HTMLDivElement>) {
    if (!wrapRef.current) return;
    const rect = wrapRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    if (x < pad.left - 4 || x > pad.left + innerW + 4) {
      setHoverDay(null);
      return;
    }
    const rawDay = ((x - pad.left) / innerW) * horizon;
    const d = Math.max(0, Math.min(horizon, Math.round(rawDay)));
    setHoverDay(d);
  }

  // Tooltip content at hoverDay
  const tooltip = useMemo(() => {
    if (hoverDay === null) return null;
    // Find nearest line sample at-or-before hoverDay (for the price line value)
    let nearestDay: number | null = null;
    for (const d of lineDays) {
      if (d <= hoverDay) nearestDay = d;
      else break;
    }
    if (nearestDay === null && lineDays.length) nearestDay = lineDays[0];
    const indexed =
      nearestDay !== null ? (dayValues.get(nearestDay) as number) : null;

    // Mentions exactly at hoverDay (rounded match)
    const dots: { handle: string; captured: number | null; tweet_ts: string }[] = [];
    for (const [handle, arr] of byHandle.entries()) {
      for (const p of arr) {
        if (Math.round(p.day) === hoverDay) {
          dots.push({ handle, captured: p.captured_ret, tweet_ts: p.tweet_ts });
        }
      }
    }
    return { indexed, dots, day: hoverDay };
  }, [hoverDay, lineDays, dayValues, byHandle]);

  const totalRetPos = token.total_return >= 0;
  const totalRetClass = totalRetPos ? "text-emerald-400" : "text-rose-400";

  return (
    <div className="rounded-lg border border-white/10 bg-surface p-3">
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-ink">
            {token.symbol ?? "?"}{" "}
            {token.name ? (
              <span className="text-xs text-muted">· {token.name}</span>
            ) : null}
          </div>
          <div className="text-[10px] text-muted">
            t0 {fmtDate(token.t0_ts)} · {horizon}d window
          </div>
        </div>
        <div className={`text-sm font-semibold tabular-nums ${totalRetClass}`}>
          {fmtPct(token.total_return, 0)}
        </div>
      </div>

      {/* Per-chart legend: which top accounts have dots on this panel. */}
      {byHandle.size > 0 ? (
        <div className="mb-1 flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] leading-tight">
          {[...byHandle.keys()].map((h) => {
            const color = handleToColor.get(h) ?? "#9aa0a6";
            const dimmed = hovered !== null && hovered !== h;
            return (
              <span
                key={h}
                className={`inline-flex items-center gap-1 ${dimmed ? "opacity-40" : ""}`}
              >
                <span
                  className="inline-block h-1.5 w-1.5 rounded-full"
                  style={{ background: color }}
                />
                <span className="text-muted">@{h}</span>
              </span>
            );
          })}
        </div>
      ) : null}

      <div
        ref={wrapRef}
        className="relative w-full"
        style={{ height: HEIGHT }}
        onMouseMove={onMove}
        onMouseLeave={() => setHoverDay(null)}
      >
        <svg
          width={width}
          height={HEIGHT}
          className="absolute inset-0 select-none"
        >
          {/* Y grid + tick labels */}
          {yTicks.map((v, i) => (
            <g key={`yt-${i}`}>
              <line
                x1={pad.left}
                x2={pad.left + innerW}
                y1={yScale(v)}
                y2={yScale(v)}
                stroke="rgba(255,255,255,0.06)"
                strokeDasharray="3 3"
              />
              <text
                x={pad.left - 4}
                y={yScale(v) + 3}
                textAnchor="end"
                fill="#9aa0a6"
                fontSize={10}
              >
                {fmtIndexed(v)}
              </text>
            </g>
          ))}

          {/* Reference line at indexed=1 (token's t0 price) */}
          {yMin <= 1 && yMax >= 1 ? (
            <line
              x1={pad.left}
              x2={pad.left + innerW}
              y1={yScale(1)}
              y2={yScale(1)}
              stroke="rgba(255,255,255,0.22)"
              strokeDasharray="2 4"
            />
          ) : null}

          {/* X ticks + labels */}
          {xTicks.map((d, i) => (
            <g key={`xt-${i}`}>
              <line
                x1={xScale(d)}
                x2={xScale(d)}
                y1={pad.top + innerH}
                y2={pad.top + innerH + 3}
                stroke="rgba(255,255,255,0.18)"
              />
              <text
                x={xScale(d)}
                y={pad.top + innerH + 14}
                textAnchor="middle"
                fill="#9aa0a6"
                fontSize={10}
              >
                d{d}
              </text>
            </g>
          ))}

          {/* Axis lines */}
          <line
            x1={pad.left}
            x2={pad.left + innerW}
            y1={pad.top + innerH}
            y2={pad.top + innerH}
            stroke="rgba(255,255,255,0.12)"
          />
          <line
            x1={pad.left}
            x2={pad.left}
            y1={pad.top}
            y2={pad.top + innerH}
            stroke="rgba(255,255,255,0.12)"
          />

          {/* Price line */}
          {linePath ? (
            <path
              d={linePath}
              fill="none"
              stroke="rgba(255,255,255,0.55)"
              strokeWidth={1.5}
            />
          ) : null}

          {/* Hover vertical cursor */}
          {hoverDay !== null ? (
            <line
              x1={xScale(hoverDay)}
              x2={xScale(hoverDay)}
              y1={pad.top}
              y2={pad.top + innerH}
              stroke="rgba(255,255,255,0.22)"
              strokeDasharray="2 4"
            />
          ) : null}

          {/* Mention dots */}
          {handlesSorted.map((h) => {
            const color = handleToColor.get(h) ?? "#9aa0a6";
            const dimmed = hovered !== null && hovered !== h;
            const arr = byHandle.get(h) ?? [];
            return (
              <g key={h} opacity={dimmed ? 0.25 : 1}>
                {arr.map((p, i) => (
                  <circle
                    key={i}
                    cx={xScale(p.day)}
                    cy={yScale(p.indexed)}
                    r={hoverDay !== null && Math.round(p.day) === hoverDay ? 5 : 4}
                    fill={color}
                    stroke="rgba(0,0,0,0.7)"
                    strokeWidth={1}
                  />
                ))}
              </g>
            );
          })}
        </svg>

        {/* HTML tooltip overlay — positioned near hover */}
        {tooltip && hoverDay !== null ? (
          <div
            className="pointer-events-none absolute z-10 rounded-md border border-white/10 bg-bg/95 px-2 py-1.5 text-[11px] shadow-lg backdrop-blur"
            style={{
              left: Math.min(
                xScale(hoverDay) + 8,
                width - 160,
              ),
              top: 4,
              minWidth: 140,
            }}
          >
            <div className="mb-0.5 text-muted">
              d{tooltip.day}
              {tooltip.indexed !== null ? (
                <span className="text-ink"> · {fmtIndexed(tooltip.indexed)}</span>
              ) : null}
            </div>
            {tooltip.dots.length > 0 ? (
              <>
                <ul className="space-y-0.5 tabular-nums">
                  {tooltip.dots.map((d, i) => {
                    const color = handleToColor.get(d.handle) ?? "#9aa0a6";
                    return (
                      <li
                        key={`${d.handle}-${i}`}
                        className="flex items-center gap-1.5"
                      >
                        <span
                          className="inline-block h-2 w-2 rounded-full"
                          style={{ background: color }}
                        />
                        <span className="text-ink">@{d.handle}</span>
                        {typeof d.captured === "number" ? (
                          <span
                            className={
                              d.captured > 0
                                ? "ml-auto text-emerald-400"
                                : d.captured < 0
                                  ? "ml-auto text-rose-400"
                                  : "ml-auto text-muted"
                            }
                          >
                            {fmtPct(d.captured, 0)}
                          </span>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
                <div className="mt-1 text-[9px] italic text-muted/70">
                  % = return from mention price to window end
                </div>
              </>
            ) : (
              <div className="text-[10px] italic text-muted/60">
                no mention on this day
              </div>
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}
