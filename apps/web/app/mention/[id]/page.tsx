import Link from "next/link";
import { notFound } from "next/navigation";
import {
  type SeriesResponse,
  fmtDate,
  getMention,
  getMentionSeries,
  pct,
  pctClass,
} from "@/lib/api";

export const revalidate = 60;

type Params = Promise<{ id: string }>;

export default async function MentionPage({ params }: { params: Params }) {
  const { id } = await params;
  const mid = Number(id);
  if (!Number.isFinite(mid)) notFound();

  let mention, series;
  try {
    [mention, series] = await Promise.all([getMention(mid), getMentionSeries(mid)]);
  } catch (e) {
    if (String(e).includes("404")) notFound();
    throw e;
  }

  const tweetUrl = `https://x.com/${mention.account.handle}/status/${mention.tweet_id}`;

  return (
    <main className="mx-auto max-w-4xl px-6 py-10 space-y-6">
      <nav className="text-sm space-x-3">
        <Link href="/" className="text-accent hover:underline">
          ← leaderboard
        </Link>
        <Link
          href={`/account/${mention.account.handle}`}
          className="text-accent hover:underline"
        >
          @{mention.account.handle}
        </Link>
      </nav>

      <header className="space-y-1">
        <div className="flex items-baseline gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">
            {mention.token.symbol ?? "(unknown token)"}
          </h1>
          {mention.token.name ? (
            <span className="text-muted">{mention.token.name}</span>
          ) : null}
        </div>
        <p className="text-xs text-muted">
          mentioned by @{mention.account.handle} on {fmtDate(mention.tweet_ts)}
          {mention.price_at_mention ? (
            <>
              {" "}· t0 = ${mention.price_at_mention.toLocaleString(undefined, {
                maximumSignificantDigits: 6,
              })}
            </>
          ) : null}
        </p>
      </header>

      <section className="rounded-lg border border-white/10 bg-surface p-5 space-y-3">
        <blockquote className="text-sm leading-relaxed">
          “{mention.tweet_text}”
        </blockquote>
        <div className="flex flex-wrap gap-3 text-xs text-muted">
          {mention.match_kind ? <span>match: {mention.match_kind}</span> : null}
          {mention.raw_match ? (
            <span>
              raw: <code className="text-ink/80">{mention.raw_match}</code>
            </span>
          ) : null}
          {mention.sentiment ? <span>sentiment: {mention.sentiment}</span> : null}
          <a
            href={tweetUrl}
            target="_blank"
            rel="noreferrer"
            className="text-accent hover:underline ml-auto"
          >
            view on x ↗
          </a>
        </div>
      </section>

      <section className="grid gap-3 grid-cols-2 sm:grid-cols-4">
        <Stat label="1d" value={mention.returns.r_1d} />
        <Stat label="7d" value={mention.returns.r_7d} />
        <Stat label="30d" value={mention.returns.r_30d} />
        <Stat label="90d" value={mention.returns.r_90d} />
        <Stat
          label="30d excess"
          value={mention.matured["30d"] ? mention.returns.r_30d_excess : null}
          subtitle={!mention.matured["30d"] ? "open" : "vs BTC"}
        />
        <Stat
          label="90d excess"
          value={mention.matured["90d"] ? mention.returns.r_90d_excess : null}
          subtitle={!mention.matured["90d"] ? "open" : "vs BTC"}
        />
        <Stat
          label="365d"
          value={mention.matured["365d"] ? mention.returns.r_365d : null}
          subtitle={!mention.matured["365d"] ? "open" : "raw"}
        />
        <Stat
          label="365d excess"
          value={mention.matured["365d"] ? mention.returns.r_365d_excess : null}
          subtitle={!mention.matured["365d"] ? "open" : "vs BTC"}
        />
      </section>

      <section className="space-y-2">
        <h2 className="text-sm uppercase tracking-wider text-muted">price series</h2>
        <PriceChart series={series} />
        <p className="text-xs text-muted">
          {series.points.length} points · t0 anchor at{" "}
          {fmtDate(series.tweet_ts)}.
        </p>
      </section>
    </main>
  );
}

function Stat({
  label,
  value,
  subtitle,
}: {
  label: string;
  value: number | null;
  subtitle?: string;
}) {
  return (
    <div className="rounded-lg border border-white/10 bg-surface p-3">
      <div className="text-xs uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-1 text-xl font-semibold tabular-nums ${pctClass(value)}`}>
        {pct(value)}
      </div>
      {subtitle ? <div className="text-xs text-muted">{subtitle}</div> : null}
    </div>
  );
}

function PriceChart({ series }: { series: SeriesResponse }) {
  const pts = series.points;
  if (pts.length < 2 || series.p0 === null) {
    return (
      <div className="rounded-lg border border-white/10 bg-surface p-6 text-sm text-muted">
        Not enough price data to chart.
      </div>
    );
  }

  const W = 800;
  const H = 220;
  const PAD = 30;

  const t0Ms = new Date(series.tweet_ts).getTime();
  const xs = pts.map((p) => new Date(p.ts).getTime());
  const ys = pts.map((p) => p.close_usd);
  const minX = Math.min(...xs, t0Ms);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys, series.p0);
  const maxY = Math.max(...ys, series.p0);
  const xScale = (x: number) =>
    PAD + ((x - minX) / Math.max(1, maxX - minX)) * (W - 2 * PAD);
  const yScale = (y: number) =>
    H - PAD - ((y - minY) / Math.max(1e-12, maxY - minY)) * (H - 2 * PAD);

  const path = pts
    .map((p, i) => {
      const x = xScale(new Date(p.ts).getTime());
      const y = yScale(p.close_usd);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const t0X = xScale(t0Ms);
  const p0Y = yScale(series.p0);

  return (
    <div className="rounded-lg border border-white/10 bg-surface p-3">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full h-auto"
        role="img"
        aria-label="price series"
      >
        <line
          x1={t0X}
          x2={t0X}
          y1={PAD}
          y2={H - PAD}
          stroke="#7dd3fc"
          strokeOpacity="0.4"
          strokeDasharray="4 4"
        />
        <line
          x1={PAD}
          x2={W - PAD}
          y1={p0Y}
          y2={p0Y}
          stroke="#7dd3fc"
          strokeOpacity="0.2"
          strokeDasharray="2 4"
        />
        <path d={path} fill="none" stroke="#7dd3fc" strokeWidth="1.5" />
        <circle cx={t0X} cy={p0Y} r="3" fill="#7dd3fc" />
        <text
          x={t0X + 6}
          y={PAD + 12}
          fontSize="10"
          fill="#9aa0a6"
        >
          t0 ${series.p0.toLocaleString(undefined, { maximumSignificantDigits: 6 })}
        </text>
        <text x={PAD} y={H - 8} fontSize="10" fill="#9aa0a6">
          {fmtDate(new Date(minX).toISOString())}
        </text>
        <text
          x={W - PAD}
          y={H - 8}
          fontSize="10"
          fill="#9aa0a6"
          textAnchor="end"
        >
          {fmtDate(new Date(maxX).toISOString())}
        </text>
      </svg>
    </div>
  );
}
