export default function HomePage() {
  return (
    <main className="mx-auto max-w-3xl px-6 py-24">
      <h1 className="text-5xl font-semibold tracking-tight">shillscore</h1>
      <p className="mt-4 text-muted">Crypto-Twitter signal accuracy. Receipts, not vibes.</p>

      <section className="mt-12 rounded-lg border border-white/10 bg-surface p-6">
        <h2 className="text-sm uppercase tracking-wider text-muted">Status</h2>
        <p className="mt-2">
          Phase 0: scaffold deployed. Leaderboard ships in Phase 2 — see{" "}
          <a className="text-accent underline" href="https://github.com/" target="_blank" rel="noreferrer">
            docs/plan.md
          </a>
          .
        </p>
      </section>
    </main>
  );
}
