import Link from "next/link";
import type { BestCall, Cohort, LeaderboardRow, View } from "@/lib/api";

type Props = {
  rows: LeaderboardRow[];
  cohort: Cohort;
  // One per top-3 row, same order. Null when the handle has no matured calls
  // in the cohort (rare — they'd not be on the leaderboard at all in that case).
  bestCalls: (BestCall | null)[];
  view: View;
};

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const s = (v * 100).toFixed(digits);
  const n = Number(s);
  return `${n > 0 ? "+" : ""}${s}%`;
}

// Bar length encodes |damped_score| relative to the top-3 maximum, so the
// visual gap matches the data gap (Tufte: lie factor ≈ 1). Floor at 6% so
// a near-zero #3 still renders a visible nub. Sign decides bar colour.
function barWidthPct(score: number | null, peak: number): number {
  if (score === null || !Number.isFinite(score) || peak <= 0) return 6;
  const w = (Math.abs(score) / peak) * 100;
  return Math.max(6, Math.min(100, w));
}

export function TopAccountsPodium({ rows, cohort, bestCalls, view }: Props) {
  const top = rows.slice(0, 3);
  if (top.length === 0) return null;
  const viewQS = view === "scouts" ? "" : `&view=${view}`;

  const peak = Math.max(
    ...top.map((r) =>
      r.damped_score !== null && Number.isFinite(r.damped_score)
        ? Math.abs(r.damped_score)
        : 0,
    ),
    1e-6,
  );

  return (
    <section className="space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm uppercase tracking-wider text-muted">
          {cohort} leaders
        </h2>
        <p className="text-[11px] text-muted">
          ranked by damped median BTC-excess · bar length = score · follow on X
        </p>
      </div>
      <ol className="space-y-1.5">
        {top.map((r, i) => {
          const best = bestCalls[i] ?? null;
          const score = r.damped_score;
          const positive = (score ?? 0) > 0;
          const w = barWidthPct(score, peak);
          const leadAccent = i === 0;
          return (
            <li
              key={r.account_id}
              className={`relative overflow-hidden rounded-lg border border-white/10 bg-surface ${
                leadAccent ? "border-l-2 border-l-accent/70" : ""
              }`}
            >
              <div
                aria-hidden
                className={`absolute inset-y-0 left-0 ${
                  positive ? "bg-emerald-400/10" : "bg-rose-400/10"
                }`}
                style={{ width: `${w}%` }}
              />
              <div className="relative flex flex-wrap items-baseline gap-x-3 gap-y-1 px-3 py-2.5">
                <span className="w-5 shrink-0 text-right text-sm font-semibold tabular-nums text-muted">
                  {i + 1}
                </span>
                <Link
                  href={`/account/${r.handle}?cohort=${cohort}${viewQS}`}
                  className="truncate text-base font-semibold text-ink hover:text-accent hover:underline"
                  title={r.display_name ?? r.handle}
                >
                  @{r.handle}
                </Link>
                <span
                  className={`text-base font-semibold tabular-nums ${
                    positive
                      ? "text-emerald-400"
                      : (score ?? 0) < 0
                        ? "text-rose-400"
                        : "text-muted"
                  }`}
                >
                  {fmtPct(score)}
                </span>
                <span className="text-[11px] text-muted">
                  n={r.n_matured}
                  {r.win_rate !== null
                    ? ` · ${Math.round(r.win_rate * 100)}% win`
                    : ""}
                </span>

                {best && best.symbol ? (
                  <Link
                    href={`/mention/${best.mention_id}`}
                    className="ml-auto inline-flex flex-wrap items-baseline gap-1.5 text-[11px] text-muted hover:text-ink"
                    title={`Best ${cohort} call: ${best.symbol} ${fmtPct(best.raw_ret, 0)} raw`}
                  >
                    <span className="text-muted/70">best call</span>
                    <span className="font-medium text-ink">{best.symbol}</span>
                    <span
                      className={`tabular-nums ${
                        (best.raw_ret ?? 0) > 0
                          ? "text-emerald-400"
                          : "text-rose-400"
                      }`}
                    >
                      {fmtPct(best.raw_ret, 0)}
                    </span>
                    {best.excess_ret !== null ? (
                      <span className="tabular-nums text-muted">
                        {fmtPct(best.excess_ret, 0)} vs BTC
                      </span>
                    ) : null}
                  </Link>
                ) : (
                  <span className="ml-auto text-[11px] text-muted/60">
                    no best call in {cohort}
                  </span>
                )}

                <a
                  href={`https://x.com/intent/follow?screen_name=${r.handle}`}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex items-center gap-1 rounded-md border border-accent/40 bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent hover:bg-accent/20"
                >
                  follow
                  <span aria-hidden>↗</span>
                </a>
              </div>
            </li>
          );
        })}
      </ol>
      <p className="text-[10px] text-muted/70">
        When you sign in with X, this strip will flag accounts you don&apos;t
        yet follow.
      </p>
    </section>
  );
}
