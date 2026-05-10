// Drop at: apps/web/app/admin/page.tsx
// Server component — fetches all four endpoints in parallel via the proxy route
// and renders dark-theme cards + tables. Auto-refreshes every 30s.

export const dynamic = "force-dynamic";
export const revalidate = 0;

type Stats = {
  counts: Record<string, number>;
  queue: { pending: number; in_progress: number; retry: number; results: number };
};
type Account = {
  handle: string;
  followers_count: number | null;
  last_synced_at: string | null;
  last_tweet_id: string | null;
  oldest_tweet_id: string | null;
  mentions: number;
};
type Mention = {
  id: number;
  tweet_id: string;
  tweet_ts: string;
  tweet_text: string;
  raw_match: string | null;
  match_kind: string | null;
  sentiment: string | null;
  handle: string;
  symbol: string | null;
  coingecko_id: string | null;
};
type Token = {
  symbol: string;
  name: string | null;
  coingecko_id: string | null;
  contract_addr: string | null;
  is_verified: boolean;
  mentions: number;
};

async function fetchAdmin<T>(path: string): Promise<T | { error: string }> {
  const base = process.env.INTERNAL_API_URL ?? "http://api:8000";
  const token = process.env.ADMIN_API_TOKEN ?? "";
  try {
    const res = await fetch(`${base}/api/admin/${path}`, {
      headers: { "X-Admin-Token": token },
      cache: "no-store",
    });
    if (!res.ok) return { error: `HTTP ${res.status}` };
    return (await res.json()) as T;
  } catch (e) {
    return { error: String(e) };
  }
}

function fmtDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toISOString().replace("T", " ").slice(0, 19);
}

function ErrorBanner({ label, error }: { label: string; error: string }) {
  return (
    <div className="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300">
      {label}: {error}
    </div>
  );
}

export default async function AdminPage() {
  const [stats, accounts, mentions, tokens] = await Promise.all([
    fetchAdmin<Stats>("stats"),
    fetchAdmin<{ rows: Account[] }>("accounts?limit=200"),
    fetchAdmin<{ rows: Mention[] }>("mentions?limit=50"),
    fetchAdmin<{ rows: Token[] }>("tokens?limit=50"),
  ]);

  return (
    <main className="mx-auto max-w-7xl px-6 py-10 space-y-8">
      <header className="flex items-baseline justify-between">
        <h1 className="text-3xl font-semibold tracking-tight">shillscore / admin</h1>
        <p className="text-xs text-muted">read-only — auto-refresh every 30s via meta tag below</p>
      </header>

      {/* Auto refresh */}
      <meta httpEquiv="refresh" content="30" />

      {/* Stats */}
      {"error" in stats ? (
        <ErrorBanner label="stats" error={stats.error} />
      ) : (
        <section className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-9">
          {Object.entries(stats.counts).map(([k, v]) => (
            <Card key={k} label={k} value={v} />
          ))}
          <Card label="queue pending" value={stats.queue.pending} accent />
          <Card label="in-progress" value={stats.queue.in_progress} accent />
          <Card label="retry" value={stats.queue.retry} accent />
          <Card label="results" value={stats.queue.results} accent />
        </section>
      )}

      {/* Mentions */}
      <section className="space-y-2">
        <h2 className="text-sm uppercase tracking-wider text-muted">recent mentions</h2>
        {"error" in mentions ? (
          <ErrorBanner label="mentions" error={mentions.error} />
        ) : mentions.rows.length === 0 ? (
          <p className="text-sm text-muted">no mentions yet</p>
        ) : (
          <Table
            cols={["ts", "handle", "symbol", "kind", "sentiment", "raw_match", "tweet"]}
            rows={mentions.rows.map((m) => [
              fmtDate(m.tweet_ts),
              <a key="h" href={`https://x.com/${m.handle}`} className="text-accent" target="_blank" rel="noreferrer">@{m.handle}</a>,
              m.symbol ?? <span className="text-muted">—</span>,
              m.match_kind ?? "—",
              m.sentiment ?? "—",
              <code key="r" className="text-xs">{m.raw_match ?? "—"}</code>,
              <span key="t" className="line-clamp-1 max-w-md text-xs text-muted">{m.tweet_text}</span>,
            ])}
          />
        )}
      </section>

      {/* Tokens */}
      <section className="space-y-2">
        <h2 className="text-sm uppercase tracking-wider text-muted">tokens by mention count</h2>
        {"error" in tokens ? (
          <ErrorBanner label="tokens" error={tokens.error} />
        ) : (
          <Table
            cols={["symbol", "name", "coingecko_id", "contract", "verified", "mentions"]}
            rows={tokens.rows.map((t) => [
              <strong key="s">{t.symbol}</strong>,
              t.name ?? "—",
              t.coingecko_id ?? "—",
              <code key="c" className="text-xs">{t.contract_addr ? `${t.contract_addr.slice(0, 8)}…${t.contract_addr.slice(-6)}` : "—"}</code>,
              t.is_verified ? "✓" : "—",
              t.mentions,
            ])}
          />
        )}
      </section>

      {/* Accounts */}
      <section className="space-y-2">
        <h2 className="text-sm uppercase tracking-wider text-muted">account sync state</h2>
        {"error" in accounts ? (
          <ErrorBanner label="accounts" error={accounts.error} />
        ) : (
          <Table
            cols={["handle", "followers", "last_synced_at", "last_tweet_id", "oldest_tweet_id", "mentions"]}
            rows={accounts.rows.map((a) => [
              <a key="h" href={`https://x.com/${a.handle}`} className="text-accent" target="_blank" rel="noreferrer">@{a.handle}</a>,
              a.followers_count ?? "—",
              fmtDate(a.last_synced_at),
              a.last_tweet_id ?? <span className="text-muted">—</span>,
              a.oldest_tweet_id ?? <span className="text-muted">—</span>,
              a.mentions,
            ])}
          />
        )}
      </section>
    </main>
  );
}

function Card({ label, value, accent }: { label: string; value: number; accent?: boolean }) {
  return (
    <div className={`rounded-lg border p-3 ${accent ? "border-accent/30 bg-accent/5" : "border-white/10 bg-surface"}`}>
      <div className="text-xs uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value.toLocaleString()}</div>
    </div>
  );
}

function Table({ cols, rows }: { cols: string[]; rows: React.ReactNode[][] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-white/10 bg-surface">
      <table className="w-full text-sm">
        <thead className="bg-white/[0.03]">
          <tr>
            {cols.map((c) => (
              <th key={c} className="px-3 py-2 text-left text-xs uppercase tracking-wider text-muted">{c}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-white/[0.06]">
          {rows.map((r, i) => (
            <tr key={i} className="hover:bg-white/[0.02]">
              {r.map((cell, j) => (
                <td key={j} className="px-3 py-2 align-top">{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
