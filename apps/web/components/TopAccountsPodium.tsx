import Link from "next/link";
import type { BestCall, Cohort, LeaderboardRow } from "@/lib/api";

type Props = {
  rows: LeaderboardRow[];
  cohort: Cohort;
  // One per top-3 row, same order. Null when the handle has no matured calls
  // in the cohort (rare — they'd not be on the leaderboard at all in that case).
  bestCalls: (BestCall | null)[];
  scouts: boolean;
};

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const s = (v * 100).toFixed(digits);
  const n = Number(s);
  return `${n > 0 ? "+" : ""}${s}%`;
}

// Rank 1/2/3 visual styling. Gold/silver/bronze are tuned for the dark
// surface — the gold isn't pure yellow because that vibrates on the navy.
const PODIUM: {
  rank: number;
  label: string;
  emoji: string;
  ring: string;
  bg: string;
  badgeBg: string;
  badgeText: string;
}[] = [
  {
    rank: 1,
    label: "1st",
    emoji: "👑",
    ring: "ring-amber-300/50",
    bg: "bg-gradient-to-b from-amber-400/15 to-amber-400/[0.04]",
    badgeBg: "bg-amber-300",
    badgeText: "text-black",
  },
  {
    rank: 2,
    label: "2nd",
    emoji: "🥈",
    ring: "ring-slate-300/40",
    bg: "bg-gradient-to-b from-slate-300/10 to-slate-300/[0.03]",
    badgeBg: "bg-slate-200",
    badgeText: "text-black",
  },
  {
    rank: 3,
    label: "3rd",
    emoji: "🥉",
    ring: "ring-orange-400/40",
    bg: "bg-gradient-to-b from-orange-400/10 to-orange-400/[0.03]",
    badgeBg: "bg-orange-400",
    badgeText: "text-black",
  },
];

export function TopAccountsPodium({ rows, cohort, bestCalls, scouts }: Props) {
  const top = rows.slice(0, 3);
  if (top.length === 0) return null;
  const scoutsQS = scouts ? "" : "&scouts=0";

  return (
    <section className="space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm uppercase tracking-wider text-muted">
          {cohort} leaders
        </h2>
        <p className="text-[11px] text-muted">
          ranked by damped median BTC-excess · follow them on X
        </p>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {top.map((r, i) => {
          const meta = PODIUM[i];
          const best = bestCalls[i] ?? null;
          return (
            <div
              key={r.account_id}
              className={`relative rounded-xl border border-white/10 p-4 ring-1 ${meta.ring} ${meta.bg}`}
            >
              <div className="absolute -top-2 left-3 flex items-center gap-1">
                <span
                  className={`inline-flex h-5 items-center gap-1 rounded-full px-2 text-[10px] font-semibold uppercase tracking-wider ${meta.badgeBg} ${meta.badgeText}`}
                >
                  <span>{meta.emoji}</span>
                  <span>{meta.label}</span>
                </span>
              </div>

              <div className="mt-1 flex items-baseline justify-between gap-2">
                <Link
                  href={`/account/${r.handle}?cohort=${cohort}${scoutsQS}`}
                  className="truncate text-base font-semibold text-ink hover:text-accent hover:underline"
                  title={r.display_name ?? r.handle}
                >
                  @{r.handle}
                </Link>
                <span
                  className={`text-base font-semibold tabular-nums ${
                    (r.damped_score ?? 0) > 0
                      ? "text-emerald-400"
                      : (r.damped_score ?? 0) < 0
                        ? "text-rose-400"
                        : "text-muted"
                  }`}
                >
                  {fmtPct(r.damped_score)}
                </span>
              </div>
              <div className="text-[11px] text-muted">
                damped score · n={r.n_matured}
                {r.win_rate !== null ? ` · ${Math.round(r.win_rate * 100)}% win` : ""}
              </div>

              {/* Best call = highest raw cohort-horizon return among matured
                  calls. Independent from the token-charts panel below — so
                  consistent dip-buyers on tokens that didn't win from day-0
                  still get a credit here. */}
              {best && best.symbol ? (
                <Link
                  href={`/mention/${best.mention_id}`}
                  className="mt-3 block rounded-md border border-white/[0.06] bg-white/[0.02] px-2 py-1.5 hover:border-white/15 hover:bg-white/[0.04]"
                >
                  <div className="text-[10px] uppercase tracking-wider text-muted">
                    best call this {cohort}
                  </div>
                  <div className="mt-0.5 flex items-baseline justify-between gap-2">
                    <span className="text-sm font-medium text-ink">
                      {best.symbol}
                    </span>
                    <span
                      className={`text-sm font-semibold tabular-nums ${
                        (best.raw_ret ?? 0) > 0
                          ? "text-emerald-400"
                          : "text-rose-400"
                      }`}
                    >
                      {fmtPct(best.raw_ret, 0)}
                    </span>
                  </div>
                  <div className="text-[10px] text-muted">
                    called {new Date(best.tweet_ts).toISOString().slice(0, 10)}
                    {best.excess_ret !== null ? (
                      <span className="ml-1">
                        · {fmtPct(best.excess_ret, 0)} vs BTC
                      </span>
                    ) : null}
                  </div>
                </Link>
              ) : (
                <div className="mt-3 rounded-md border border-white/[0.06] bg-white/[0.02] px-2 py-1.5 text-[11px] text-muted/70">
                  no matured calls in {cohort}
                </div>
              )}

              <div className="mt-3 flex items-center gap-2">
                <a
                  href={`https://x.com/intent/follow?screen_name=${r.handle}`}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex items-center gap-1.5 rounded-md border border-accent/40 bg-accent/10 px-2.5 py-1 text-[11px] font-medium text-accent hover:bg-accent/20"
                >
                  <span>follow on X</span>
                  <span aria-hidden>↗</span>
                </a>
                <Link
                  href={`/account/${r.handle}?cohort=${cohort}${scoutsQS}`}
                  className="text-[11px] text-muted hover:text-ink hover:underline"
                >
                  view calls →
                </Link>
              </div>
            </div>
          );
        })}
      </div>
      <p className="text-[10px] text-muted/70">
        When you sign in with X, this strip will flag accounts you don&apos;t
        yet follow. For now the follow links open X in a new tab.
      </p>
    </section>
  );
}
