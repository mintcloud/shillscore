import Link from "next/link";
import {
  type BestCall,
  type Cohort,
  type Sort,
  type TokenChartsResponse,
  getAccountBestCall,
  getLeaderboard,
  getLeaderboardCurves,
  getTokenCharts,
  pct,
  pctClass,
} from "@/lib/api";
import { LeaderboardCurvesChart } from "@/components/LeaderboardCurvesChart";
import { TokenChartsGrid } from "@/components/TokenChartsGrid";
import { TopAccountsPodium } from "@/components/TopAccountsPodium";

export const revalidate = 60;

const COHORTS: Cohort[] = ["30d", "90d", "365d"];
const SORTS: Sort[] = ["excess", "raw"];

const COHORT_BLURB: Record<Cohort, string> = {
  "30d":
    "30-day BTC-excess. Active default — all current data sits inside this window.",
  "90d":
    "90-day BTC-excess. Sparse today; fills out as seed mentions cross the 90-day mark.",
  "365d":
    "Annual BTC-excess. Empty until 2027 — first calls won't close until then.",
};

type SP = Promise<{ cohort?: string; sort?: string; scouts?: string }>;

function parseCohort(v: string | undefined): Cohort {
  return v === "90d" || v === "365d" ? v : "30d";
}
function parseSort(v: string | undefined): Sort {
  return v === "raw" ? "raw" : "excess";
}
function parseScouts(v: string | undefined): boolean {
  return v === "1" || v === "true";
}

export default async function LeaderboardPage({ searchParams }: { searchParams: SP }) {
  const sp = await searchParams;
  const cohort = parseCohort(sp.cohort);
  const sort = parseSort(sp.sort);
  const scouts = parseScouts(sp.scouts);

  let rows = [] as Awaited<ReturnType<typeof getLeaderboard>>["rows"];
  let error: string | null = null;
  let curves: Awaited<ReturnType<typeof getLeaderboardCurves>> | null = null;
  let tokenCharts: TokenChartsResponse | null = null;
  let bestCalls: (BestCall | null)[] = [];
  // Token-charts view only makes sense for 30d/90d — 365d has no matured
  // calls yet (seed mentions from Feb 2026 won't mature until Feb 2027).
  const wantsTokenCharts = cohort === "30d" || cohort === "90d";
  try {
    const baseTasks = [
      getLeaderboard(cohort, sort, scouts),
      getLeaderboardCurves(cohort, 10, scouts),
    ] as const;
    if (wantsTokenCharts) {
      const [r, c, tc] = await Promise.all([
        ...baseTasks,
        getTokenCharts(cohort as "30d" | "90d", 9, 10, scouts),
      ]);
      rows = r.rows;
      curves = c;
      tokenCharts = tc;
    } else {
      const [r, c] = await Promise.all(baseTasks);
      rows = r.rows;
      curves = c;
    }
    // Pull the actual best matured call for each top-3 handle in parallel.
    // Using r_30d/r_90d/r_365d directly so this is independent of whatever
    // tokens happen to populate the chart grid below.
    const top3 = rows.slice(0, 3);
    bestCalls = await Promise.all(
      top3.map((r) =>
        getAccountBestCall(r.handle, cohort, scouts)
          .then((x) => x.best_call)
          .catch(() => null),
      ),
    );
  } catch (e) {
    error = String(e);
  }

  return (
    <main className="mx-auto max-w-6xl px-6 py-10 space-y-6">
      <header className="space-y-1">
        <h1 className="text-3xl font-semibold tracking-tight">shillscore</h1>
        <p className="text-sm text-muted">
          Crypto-Twitter signal accuracy. BTC-excess returns on every token
          mention, ranked by damped median.
        </p>
      </header>

      <nav className="flex flex-wrap gap-2 text-sm">
        {COHORTS.map((c) => (
          <Link
            key={c}
            href={`/?cohort=${c}&sort=${sort}${scouts ? "&scouts=1" : ""}`}
            className={`rounded-md px-3 py-1.5 border ${
              c === cohort
                ? "border-accent bg-accent/10 text-accent"
                : "border-white/10 text-muted hover:text-ink hover:border-white/30"
            }`}
          >
            {c}
          </Link>
        ))}
        <span className="mx-2 text-white/10">|</span>
        {SORTS.map((s) => (
          <Link
            key={s}
            href={`/?cohort=${cohort}&sort=${s}${scouts ? "&scouts=1" : ""}`}
            className={`rounded-md px-3 py-1.5 border ${
              s === sort
                ? "border-accent bg-accent/10 text-accent"
                : "border-white/10 text-muted hover:text-ink hover:border-white/30"
            }`}
          >
            sort: {s === "excess" ? "BTC-excess" : "raw return"}
          </Link>
        ))}
        <span className="mx-2 text-white/10">|</span>
        <Link
          href={`/?cohort=${cohort}&sort=${sort}${scouts ? "" : "&scouts=1"}`}
          title={
            scouts
              ? "Scouts mode on — each handle's top-mentioned token is dropped from their score. Project accounts whose entire signal was their own bag fall off; diversified callers survive."
              : "Default view — every matured call counts. Turn on Scouts mode to exclude each handle's #1 token (kills the project-account self-shill bias)."
          }
          className={`rounded-md px-3 py-1.5 border ${
            scouts
              ? "border-accent bg-accent/10 text-accent"
              : "border-white/10 text-muted hover:text-ink hover:border-white/30"
          }`}
        >
          {scouts ? "scouts: on" : "scouts: off"}
        </Link>
      </nav>

      <p className="text-xs text-muted">{COHORT_BLURB[cohort]}</p>
      {scouts ? (
        <p className="text-xs text-accent/80">
          Scouts mode: each handle&apos;s top-mentioned token is dropped before
          scoring. Project accounts whose signal was their own bag disappear;
          handles whose remaining calls are still good rise.
        </p>
      ) : null}

      {error ? (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300">
          API error: {error}
        </div>
      ) : rows.length === 0 ? (
        <EmptyState cohort={cohort} scouts={scouts} />
      ) : (
        <>
          <TopAccountsPodium
            rows={rows}
            cohort={cohort}
            bestCalls={bestCalls}
          />
          {curves && curves.accounts.length > 0 ? (
            <LeaderboardCurvesChart data={curves} />
          ) : null}
          {tokenCharts && tokenCharts.tokens.length > 0 ? (
            <TokenChartsGrid data={tokenCharts} />
          ) : null}
          <Table rows={rows} cohort={cohort} sort={sort} scouts={scouts} />
        </>
      )}

      <footer className="pt-6 text-xs text-muted">
        Damped score = median × √(N / (N + 5)). Min N = 5. Matured calls only —
        a mention matures into the {cohort} cohort once {cohort} have elapsed
        since the tweet, i.e. the {cohort} return window has finished.
        {scouts
          ? " Scouts mode: each handle's #1-most-mentioned token is excluded from their aggregates."
          : null}
      </footer>
    </main>
  );
}

function EmptyState({ cohort, scouts }: { cohort: Cohort; scouts: boolean }) {
  return (
    <div className="rounded-lg border border-white/10 bg-surface p-6 text-sm text-muted">
      <p>
        No matured calls in the {cohort} cohort
        {scouts ? " (scouts mode)" : ""} yet.
      </p>
      {cohort === "365d" ? (
        <p className="mt-2">
          Earliest seed mention is from Feb 2026 — first 365d closes land around
          Feb 2027. Switch to{" "}
          <Link
            href={`/?cohort=30d&sort=excess${scouts ? "&scouts=1" : ""}`}
            className="text-accent"
          >
            30d
          </Link>
          .
        </p>
      ) : (
        <p className="mt-2">
          Try{" "}
          <Link
            href={`/?cohort=30d&sort=excess${scouts ? "&scouts=1" : ""}`}
            className="text-accent"
          >
            30d
          </Link>{" "}
          — that's where current data lives.
        </p>
      )}
      {scouts ? (
        <p className="mt-2">
          Or turn{" "}
          <Link
            href={`/?cohort=${cohort}&sort=excess`}
            className="text-accent"
          >
            scouts mode off
          </Link>{" "}
          to see the full leaderboard.
        </p>
      ) : null}
    </div>
  );
}

function Table({
  rows,
  cohort,
  sort,
  scouts,
}: {
  rows: Awaited<ReturnType<typeof getLeaderboard>>["rows"];
  cohort: Cohort;
  sort: Sort;
  scouts: boolean;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-white/10 bg-surface">
      <table className="w-full text-sm">
        <thead className="bg-white/[0.03]">
          <tr className="text-xs uppercase tracking-wider text-muted">
            <th className="px-3 py-2 text-right">#</th>
            <th className="px-3 py-2 text-left">handle</th>
            <th
              className="px-3 py-2 text-right"
              title={`matured calls — mentions with at least ${cohort} elapsed since the tweet`}
            >
              matured
            </th>
            <th className="px-3 py-2 text-right">win %</th>
            <th className="px-3 py-2 text-right">
              median {sort === "excess" ? "excess" : "raw"}
            </th>
            <th className="px-3 py-2 text-right">damped</th>
            <th className="px-3 py-2 text-left">followers</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-white/[0.06]">
          {rows.map((r, i) => {
            const median = sort === "excess" ? r.median_excess : r.median_raw;
            return (
              <tr key={r.account_id} className="hover:bg-white/[0.02]">
                <td className="px-3 py-2 text-right tabular-nums text-muted">{i + 1}</td>
                <td className="px-3 py-2">
                  <Link
                    href={`/account/${r.handle}?cohort=${cohort}${scouts ? "&scouts=1" : ""}`}
                    className="text-accent hover:underline"
                  >
                    @{r.handle}
                  </Link>
                  {r.display_name ? (
                    <span className="ml-2 text-muted">{r.display_name}</span>
                  ) : null}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">{r.n_matured}</td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {r.win_rate === null ? "—" : `${(r.win_rate * 100).toFixed(0)}%`}
                </td>
                <td className={`px-3 py-2 text-right tabular-nums ${pctClass(median)}`}>
                  {pct(median)}
                </td>
                <td className={`px-3 py-2 text-right tabular-nums ${pctClass(r.damped_score)}`}>
                  {pct(r.damped_score)}
                </td>
                <td className="px-3 py-2 text-muted tabular-nums">
                  {r.followers ? r.followers.toLocaleString() : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
