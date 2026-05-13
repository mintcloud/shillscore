import Link from "next/link";
import { notFound } from "next/navigation";
import {
  type Cohort,
  fmtDate,
  getAccount,
  getAccountMentionCurves,
  pct,
  pctClass,
} from "@/lib/api";
import { AccountMentionsChart } from "@/components/AccountMentionsChart";

export const revalidate = 60;

const COHORTS: Cohort[] = ["30d", "90d", "365d"];

function parseCohort(v: string | undefined): Cohort {
  return v === "90d" || v === "365d" ? v : "30d";
}

type Params = Promise<{ handle: string }>;
type SP = Promise<{ cohort?: string }>;

export default async function AccountPage({
  params,
  searchParams,
}: {
  params: Params;
  searchParams: SP;
}) {
  const { handle } = await params;
  const sp = await searchParams;
  const cohort = parseCohort(sp.cohort);

  let data;
  let curvesData: Awaited<ReturnType<typeof getAccountMentionCurves>> | null = null;
  try {
    [data, curvesData] = await Promise.all([
      getAccount(handle),
      // 365d has no matured calls yet — skip the curves fetch.
      cohort === "365d"
        ? Promise.resolve(null)
        : getAccountMentionCurves(handle, cohort).catch(() => null),
    ]);
  } catch (e) {
    if (String(e).includes("404")) notFound();
    throw e;
  }

  const { account, cohorts, mentions } = data;

  return (
    <main className="mx-auto max-w-6xl px-6 py-10 space-y-6">
      <nav className="text-sm">
        <Link href={`/?cohort=${cohort}`} className="text-accent hover:underline">
          ← leaderboard
        </Link>
      </nav>

      <header className="space-y-1">
        <h1 className="text-3xl font-semibold tracking-tight">@{account.handle}</h1>
        {account.display_name ? (
          <p className="text-muted">{account.display_name}</p>
        ) : null}
        <p className="text-xs text-muted">
          {account.followers ? `${account.followers.toLocaleString()} followers · ` : ""}
          last synced {fmtDate(account.last_synced_at)} · lookback{" "}
          {account.lookback_days}d
        </p>
      </header>

      <nav className="flex flex-wrap gap-2 text-sm">
        {COHORTS.map((c) => (
          <Link
            key={c}
            href={`/account/${account.handle}?cohort=${c}`}
            className={`rounded-md px-3 py-1.5 border ${
              c === cohort
                ? "border-accent bg-accent/10 text-accent"
                : "border-white/10 text-muted hover:text-ink hover:border-white/30"
            }`}
          >
            {c}
          </Link>
        ))}
      </nav>

      <section className="grid gap-3 sm:grid-cols-3">
        {COHORTS.map((c) => {
          const s = cohorts[c];
          const isActive = c === cohort;
          return (
            <div
              key={c}
              className={`rounded-lg border p-4 space-y-1 ${
                isActive
                  ? "border-accent/50 bg-accent/[0.04]"
                  : "border-white/10 bg-surface"
              }`}
            >
              <div className="text-xs uppercase tracking-wider text-muted">
                {c} cohort
              </div>
              {s ? (
                <>
                  <div className="text-2xl font-semibold tabular-nums">
                    <span className={pctClass(s.median_excess)}>{pct(s.median_excess)}</span>
                  </div>
                  <div className="text-xs text-muted">
                    median excess · n={s.n_matured} · win{" "}
                    {s.win_rate !== null
                      ? `${(s.win_rate * 100).toFixed(0)}%`
                      : "—"}
                  </div>
                  <div className="text-xs text-muted">
                    damped {pct(s.damped_score)}
                  </div>
                </>
              ) : (
                <div className="text-sm text-muted">no matured calls</div>
              )}
            </div>
          );
        })}
      </section>

      {curvesData && curvesData.mentions.length > 0 ? (
        <section>
          <AccountMentionsChart data={curvesData} />
        </section>
      ) : cohort === "365d" ? (
        <p className="text-sm text-muted">
          No 365d-matured calls yet — seed mentions from Feb 2026 won't mature until Feb 2027.
        </p>
      ) : null}

      <section className="space-y-2">
        <h2 className="text-sm uppercase tracking-wider text-muted">
          mentions ({mentions.length})
        </h2>
        {mentions.length === 0 ? (
          <p className="text-sm text-muted">no mentions seeded for this account</p>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-white/10 bg-surface">
            <table className="w-full text-sm">
              <thead className="bg-white/[0.03]">
                <tr className="text-xs uppercase tracking-wider text-muted">
                  <th className="px-3 py-2 text-left">date</th>
                  <th className="px-3 py-2 text-left">token</th>
                  <th className="px-3 py-2 text-right">1d</th>
                  <th className="px-3 py-2 text-right">7d</th>
                  <th className="px-3 py-2 text-right">30d</th>
                  <th className="px-3 py-2 text-right">30d excess</th>
                  <th className="px-3 py-2 text-right">90d excess</th>
                  <th className="px-3 py-2 text-left">tweet</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/[0.06]">
                {mentions.map((m) => (
                  <tr key={m.id} className="hover:bg-white/[0.02]">
                    <td className="px-3 py-2 text-muted tabular-nums">
                      <Link href={`/mention/${m.id}`} className="text-accent hover:underline">
                        {fmtDate(m.tweet_ts)}
                      </Link>
                    </td>
                    <td className="px-3 py-2">
                      {m.symbol ? (
                        <span className="font-medium">{m.symbol}</span>
                      ) : m.raw_match ? (
                        <code className="text-xs text-muted">{m.raw_match}</code>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </td>
                    <td className={`px-3 py-2 text-right tabular-nums ${pctClass(m.returns.r_1d)}`}>
                      {pct(m.returns.r_1d)}
                    </td>
                    <td className={`px-3 py-2 text-right tabular-nums ${pctClass(m.returns.r_7d)}`}>
                      {pct(m.returns.r_7d)}
                    </td>
                    <td className={`px-3 py-2 text-right tabular-nums ${pctClass(m.returns.r_30d)}`}>
                      {pct(m.returns.r_30d)}
                    </td>
                    <td
                      className={`px-3 py-2 text-right tabular-nums ${pctClass(m.returns.r_30d_excess)}`}
                    >
                      {m.matured["30d"] ? pct(m.returns.r_30d_excess) : <span className="text-muted">open</span>}
                    </td>
                    <td
                      className={`px-3 py-2 text-right tabular-nums ${pctClass(m.returns.r_90d_excess)}`}
                    >
                      {m.matured["90d"] ? pct(m.returns.r_90d_excess) : <span className="text-muted">open</span>}
                    </td>
                    <td className="px-3 py-2 text-xs text-muted">
                      <span className="line-clamp-1 max-w-md">{m.tweet_text}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  );
}
