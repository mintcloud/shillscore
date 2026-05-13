import Link from "next/link";
import { notFound } from "next/navigation";
import {
  fmtDate,
  getAccount,
  getAccountMentionCurves,
  pct,
  pctClass,
} from "@/lib/api";
import { AccountMentionsChart } from "@/components/AccountMentionsChart";

export const revalidate = 60;

type Params = Promise<{ handle: string }>;

export default async function AccountPage({ params }: { params: Params }) {
  const { handle } = await params;

  let data;
  let curvesData: Awaited<ReturnType<typeof getAccountMentionCurves>> | null = null;
  try {
    [data, curvesData] = await Promise.all([
      getAccount(handle),
      getAccountMentionCurves(handle, "30d").catch(() => null),
    ]);
  } catch (e) {
    if (String(e).includes("404")) notFound();
    throw e;
  }

  const { account, cohorts, mentions } = data;

  return (
    <main className="mx-auto max-w-6xl px-6 py-10 space-y-6">
      <nav className="text-sm">
        <Link href="/" className="text-accent hover:underline">
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

      <section className="grid gap-3 sm:grid-cols-3">
        {(["30d", "90d", "365d"] as const).map((c) => {
          const s = cohorts[c];
          return (
            <div
              key={c}
              className="rounded-lg border border-white/10 bg-surface p-4 space-y-1"
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
