"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { EquityCurvePoint, LeaderboardCurvesResponse } from "@/lib/api";
import { CHART_PALETTE } from "@/lib/palette";
import { TweetEmbedCard } from "@/components/TweetEmbedCard";
import { loadTwitterWidgets } from "@/lib/twitter-widgets";

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

function pctClass(v: number | null | undefined) {
  if (v === null || v === undefined || !Number.isFinite(v)) return "text-muted";
  if (v > 0) return "text-emerald-400";
  if (v < 0) return "text-rose-400";
  return "text-muted";
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

// Which dot the user has clicked open. Keyed by handle + the point's epoch ts.
type SelectedDot = { handle: string; ts: number };

export function LeaderboardCurvesChart({ data }: Props) {
  const [pinned, setPinned] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [selected, setSelected] = useState<SelectedDot | null>(null);
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  // Preload widgets.js once mounted so the first dot-click doesn't pay the
  // ~30KB script latency before the X card renders. Fire-and-forget.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const idle = (window as Window & {
      requestIdleCallback?: (cb: () => void) => number;
    }).requestIdleCallback;
    const fire = () => loadTwitterWidgets().catch(() => undefined);
    if (idle) idle(fire);
    else setTimeout(fire, 500);
  }, []);

  const accounts = data.accounts;

  const { rows, lines, pointIndex, yTicks, yDomain, xDomain, xTicks } = useMemo(() => {
    if (accounts.length === 0) {
      const now = Date.now();
      return {
        rows: [],
        lines: [] as { handle: string; color: string; final: number; n: number; median: number | null; damped: number }[],
        pointIndex: new Map<string, Map<number, EquityCurvePoint>>(),
        yTicks: [-0.05, 0, 0.05],
        yDomain: [-0.05, 0.05] as [number, number],
        xDomain: [now - 86_400_000, now] as [number, number],
        xTicks: [] as number[],
      };
    }
    const today = Date.now();
    // Build a per-account map: ts -> cum_mean at each real event, plus a
    // synthetic "today" anchor at the account's last value so the curve
    // visibly extends to today. `pointIndex` keeps the full mention payload
    // per (handle, ts) so a clicked dot can look up its tweet + token.
    const eventsByHandle = new Map<string, Map<number, number>>();
    const pointIndex = new Map<string, Map<number, EquityCurvePoint>>();
    for (const a of accounts) {
      const m = new Map<number, number>();
      const pm = new Map<number, EquityCurvePoint>();
      for (const p of a.curve) {
        const t = new Date(p.ts).getTime();
        m.set(t, p.cum_mean);
        pm.set(t, p);
      }
      const last = a.curve[a.curve.length - 1]?.cum_mean;
      if (last !== undefined && !m.has(today)) m.set(today, last);
      eventsByHandle.set(a.handle, m);
      pointIndex.set(a.handle, pm);
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

    return {
      rows,
      lines,
      pointIndex,
      yTicks,
      yDomain,
      xDomain: [xMin, xMax] as [number, number],
      xTicks,
    };
  }, [accounts]);

  // The mention payload behind the currently-selected dot.
  const selectedDetail = useMemo(() => {
    if (!selected) return null;
    const point = pointIndex.get(selected.handle)?.get(selected.ts);
    if (!point) return null;
    const acc = accounts.find((a) => a.handle === selected.handle);
    const line = lines.find((l) => l.handle === selected.handle);
    return {
      handle: selected.handle,
      color: line?.color ?? "#9aa0a6",
      total: acc?.curve.length ?? point.n,
      point,
    };
  }, [selected, pointIndex, accounts, lines]);

  if (accounts.length === 0) {
    return (
      <div className="rounded-lg border border-white/10 bg-surface p-6 text-sm text-muted">
        No equity-curve data for this cohort yet.
      </div>
    );
  }

  const activeHandle = pinned ?? hovered;

  // Clicking a dot opens (or toggles shut) its mention card and isolates
  // that account's line so the dot is easy to find again.
  function pickDot(handle: string, ts: number) {
    setSelected((cur) =>
      cur && cur.handle === handle && cur.ts === ts
        ? null
        : { handle, ts },
    );
    setPinned(handle);
  }

  // Render z-order: draw the active line last so its (clickable) dots sit on
  // top of the others when curves overlap.
  const orderedLines = activeHandle
    ? [...lines].sort(
        (a, b) =>
          (a.handle === activeHandle ? 1 : 0) -
          (b.handle === activeHandle ? 1 : 0),
      )
    : lines;

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm uppercase tracking-wider text-muted">
          how the leaderboard is built
        </h2>
        <p className="text-xs text-muted">
          running mean BTC-excess per account · each dot = one matured call ·
          curve ends today
        </p>
      </div>
      <div className="relative rounded-lg border border-white/10 bg-surface p-3">
        <div className="h-[380px] w-full">
          {mounted ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={rows}
              margin={{ top: 12, right: 96, left: 4, bottom: 8 }}
              onClick={() => {
                setPinned(null);
                setSelected(null);
              }}
            >
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
              {orderedLines.map((l) => {
                const isActive = activeHandle === l.handle;
                const anyActive = activeHandle !== null;
                // Tufte layering: top-3 forward by default; ranks 4-10 recede.
                // Lookup against original `lines` (rank order) — orderedLines
                // shuffles for z-order only.
                const rank = lines.findIndex((x) => x.handle === l.handle);
                const opacity = !anyActive
                  ? rank < 3
                    ? 0.95
                    : 0.35
                  : isActive
                    ? 1
                    : 0.15;
                const width = isActive ? 2.6 : rank < 3 ? 1.8 : 1.2;
                const pointsForLine = pointIndex.get(l.handle);
                return (
                  <Line
                    key={l.handle}
                    type="monotone"
                    dataKey={l.handle}
                    stroke={l.color}
                    strokeWidth={width}
                    strokeOpacity={opacity}
                    // Each dot is a matured call. Custom renderer makes the
                    // real-mention dots clickable (with an oversized invisible
                    // hit target since the visible dots are tiny) — clicking
                    // opens that call's tweet + score-impact card below.
                    dot={(dotProps) =>
                      renderCurveDot(dotProps, {
                        color: l.color,
                        opacity,
                        isActive,
                        points: pointsForLine,
                        selectedTs:
                          selected?.handle === l.handle ? selected.ts : null,
                        onPick: (ts) => pickDot(l.handle, ts),
                        endTs: xDomain[1],
                        handle: l.handle,
                      })
                    }
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

      {/* Selected-call card — the mention behind the clicked dot. */}
      {selectedDetail ? (
        <SelectedCallCard
          handle={selectedDetail.handle}
          color={selectedDetail.color}
          total={selectedDetail.total}
          point={selectedDetail.point}
          cohort={data.cohort}
          onClose={() => setSelected(null)}
        />
      ) : (
        <p className="rounded-lg border border-dashed border-white/10 bg-surface/50 px-3 py-2 text-[11px] text-muted">
          Every dot is one matured call. <span className="text-ink">Click a dot</span> to
          see the tweet, the token it called, and how much that call moved the
          account&apos;s line.
        </p>
      )}

      {/* Reader's note — appears before the score table so the three columns
          are explained first. */}
      <p className="text-[11px] text-muted">
        Why three numbers? The chart plots the running{" "}
        <span className="text-ink">raw mean</span> — easy to read off as a
        curve but volatile. The leaderboard sorts by{" "}
        <span className="text-ink">dampened</span> = median × √(N / (N + 5)),
        which trims tails and penalises thin samples. Hover or click a row to
        isolate that account.
      </p>

      {/* Score-comparison strip — colour is reserved for the damped column,
          which is the actual ranking key. */}
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
                  <td className="px-2 py-1.5 text-right tabular-nums text-ink">
                    {fmtPct(l.final)}
                  </td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-ink">
                    {fmtPct(l.median)}
                  </td>
                  <td
                    className={`px-2 py-1.5 text-right tabular-nums font-semibold ${
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
    </div>
  );
}

// ---------------------------------------------------------------------------
// Custom dot renderer for the equity curves.
//
// Recharts hands this every point of a line (see component/Dots.js): real
// mention points get a clickable dot with an oversized transparent hit
// circle (the visible dot is only ~2.5px); the synthetic "today" anchor and
// null gaps get a plain dot / nothing. `points` is the per-handle index keyed
// on epoch ts — a hit there = a real mention, a miss = the today anchor.
// ---------------------------------------------------------------------------
type CurveDotInput = {
  cx?: number;
  cy?: number;
  value?: number | null;
  payload?: { ts?: number } | null;
};

function renderCurveDot(
  props: CurveDotInput,
  opts: {
    color: string;
    opacity: number;
    isActive: boolean;
    points: Map<number, EquityCurvePoint> | undefined;
    selectedTs: number | null;
    onPick: (ts: number) => void;
    endTs: number;
    handle: string;
  },
) {
  const { cx, cy, payload } = props;
  if (
    typeof cx !== "number" ||
    typeof cy !== "number" ||
    !payload ||
    typeof payload.ts !== "number"
  ) {
    return <g />;
  }
  const ts = payload.ts;
  const mention = opts.points?.get(ts);
  // Endpoint = rightmost ts for this account. Direct-label `@handle` there
  // so the chart self-explains instead of needing a colour legend lookup.
  const isEndpoint = ts === opts.endTs;
  const endpointLabel = isEndpoint ? (
    <text
      x={cx + 6}
      y={cy + 3}
      fill={opts.color}
      fillOpacity={Math.max(opts.opacity, 0.7)}
      fontSize={10}
      fontWeight={opts.isActive ? 600 : 500}
      style={{ pointerEvents: "none" }}
    >
      @{opts.handle}
    </text>
  ) : null;
  // No mention behind it (the "today" anchor) → plain, non-interactive dot.
  if (!mention) {
    return (
      <g>
        <circle
          cx={cx}
          cy={cy}
          r={opts.isActive ? 3 : 2.2}
          fill={opts.color}
          fillOpacity={opts.opacity}
          stroke="rgba(0,0,0,0.6)"
          strokeWidth={0.5}
        />
        {endpointLabel}
      </g>
    );
  }
  const isSelected = opts.selectedTs === ts;
  const r = isSelected ? 5.5 : opts.isActive ? 3.5 : 2.6;
  return (
    <g
      style={{ cursor: "pointer" }}
      onClick={(e) => {
        e.stopPropagation();
        opts.onPick(ts);
      }}
    >
      {/* Oversized invisible hit target — the visible dots are tiny. */}
      <circle cx={cx} cy={cy} r={9} fill="transparent" />
      <circle
        cx={cx}
        cy={cy}
        r={r}
        fill={opts.color}
        fillOpacity={opts.opacity}
        stroke={isSelected ? "#ffffff" : "rgba(0,0,0,0.6)"}
        strokeWidth={isSelected ? 1.6 : 0.5}
      />
      {endpointLabel}
    </g>
  );
}

// ---------------------------------------------------------------------------
// Selected-call card — renders the mention behind a clicked dot: the tweet
// itself, the token it called, and how that one call moved the line.
// ---------------------------------------------------------------------------
function StatRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-white/[0.06] pb-1.5">
      <span className="text-[10px] uppercase tracking-wider text-muted">
        {label}
      </span>
      <span className="text-right">{children}</span>
    </div>
  );
}

function SelectedCallCard({
  handle,
  color,
  total,
  point,
  cohort,
  onClose,
}: {
  handle: string;
  color: string;
  total: number;
  point: EquityCurvePoint;
  cohort: string;
  onClose: () => void;
}) {
  const nudge = point.cum_mean - point.mean_before;
  const tokenLabel = point.symbol
    ? `$${point.symbol.toUpperCase()}`
    : "unknown token";
  return (
    <div className="rounded-lg border border-white/10 bg-surface p-3">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="inline-flex items-center gap-1.5 text-sm">
            <span
              className="inline-block h-2.5 w-2.5 rounded-full"
              style={{ background: color }}
            />
            <Link
              href={`/account/${handle}`}
              className="font-medium text-ink hover:text-accent hover:underline"
            >
              @{handle}
            </Link>
          </span>
          <span className="text-xs text-muted">
            call {point.n} of {total} ·{" "}
            {fmtDate(new Date(point.ts).getTime())}
          </span>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="shrink-0 rounded border border-white/10 px-1.5 py-0.5 text-[10px] text-muted hover:bg-white/5 hover:text-ink"
        >
          close ✕
        </button>
      </div>

      <div className="grid gap-3 sm:grid-cols-[minmax(0,360px)_1fr]">
        {/* The rendered mention */}
        <div className="min-w-0">
          <TweetEmbedCard
            tweetId={point.tweet_id}
            handle={handle}
            oembedHtml={point.oembed_html}
            oembedError={point.oembed_error}
            tweetText={point.tweet_text}
            tweetTs={point.ts}
          />
        </div>

        {/* Token + score impact */}
        <div className="space-y-2 text-xs">
          <StatRow label="Token called">
            <Link
              href={`/mention/${point.mention_id}`}
              className="font-medium text-ink hover:text-accent hover:underline"
            >
              {tokenLabel}
            </Link>
          </StatRow>
          <StatRow label={`What the call yielded (${cohort})`}>
            <span className={`font-medium tabular-nums ${pctClass(point.last_excess)}`}>
              {fmtPct(point.last_excess)}{" "}
              <span className="text-[10px] font-normal text-muted">
                BTC-excess
              </span>
            </span>
          </StatRow>
          <StatRow label="Effect on the line">
            <span className="tabular-nums text-ink">
              {fmtPct(point.mean_before)} → {fmtPct(point.cum_mean)}{" "}
              <span className={pctClass(nudge)}>
                ({fmtPct(nudge, 2)})
              </span>
            </span>
          </StatRow>
          <p className="text-[10px] leading-snug text-muted">
            {point.n === 1 ? (
              <>
                First matured call — it set @{handle}&apos;s running-mean
                curve at its starting value of{" "}
                <span className={pctClass(point.cum_mean)}>
                  {fmtPct(point.cum_mean)}
                </span>
                .
              </>
            ) : (
              <>
                This call returned{" "}
                <span className={pctClass(point.last_excess)}>
                  {fmtPct(point.last_excess)}
                </span>{" "}
                vs BTC —{" "}
                {point.last_excess >= point.mean_before ? "above" : "below"}{" "}
                the {fmtPct(point.mean_before)} running mean at the time — so it
                pulled the line {nudge >= 0 ? "up" : "down"} by{" "}
                {fmtPct(Math.abs(nudge), 2)} to{" "}
                <span className={pctClass(point.cum_mean)}>
                  {fmtPct(point.cum_mean)}
                </span>
                .
              </>
            )}
          </p>
        </div>
      </div>
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
