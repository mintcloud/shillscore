// Server-side fetch helper for the public API. Used by RSC pages.
const BASE = process.env.INTERNAL_API_URL ?? "http://api:8000";

export type Cohort = "30d" | "90d" | "365d";
export type Sort = "excess" | "raw";
// Path A — concentration split. scouts = diversified (top token < 50% of
// matured calls); insiders = score leans on one bag (>= 50%); all = unfiltered.
export type View = "scouts" | "insiders" | "all";

export type LeaderboardRow = {
  account_id: number;
  handle: string;
  display_name: string | null;
  followers: number | null;
  n_matured: number;
  n_winners: number;
  win_rate: number | null;
  median_excess: number | null;
  median_raw: number | null;
  mean_excess: number | null;
  damped_score: number | null;
  ci_low_excess: number | null;
  ci_high_excess: number | null;
  // Concentration — fraction of this handle's matured cohort calls on its
  // single most-mentioned token, that token's symbol, and the distinct-token
  // count. is_scout = top_token_share below the threshold.
  n_distinct_tokens: number;
  top_token_symbol: string | null;
  top_token_share: number | null;
  is_scout: boolean;
};

export type Concentration = {
  n_distinct_tokens: number;
  top_token_symbol: string | null;
  top_token_share: number | null;
  is_scout: boolean;
};

export type Returns = {
  r_1d: number | null;
  r_7d: number | null;
  r_30d: number | null;
  r_90d: number | null;
  r_365d: number | null;
  r_30d_excess: number | null;
  r_90d_excess: number | null;
  r_365d_excess: number | null;
};

export type AccountMention = {
  id: number;
  tweet_id: string;
  tweet_ts: string;
  tweet_text: string;
  raw_match: string | null;
  match_kind: string | null;
  sentiment: string | null;
  price_at_mention: number | null;
  symbol: string | null;
  coingecko_id: string | null;
  contract_addr: string | null;
  returns: Returns;
  matured: { "30d": boolean; "90d": boolean; "365d": boolean };
};

export type AccountResponse = {
  account: {
    handle: string;
    display_name: string | null;
    followers: number | null;
    last_synced_at: string | null;
    lookback_days: number;
    first_seen_at: string | null;
  };
  concentration_threshold: number;
  cohorts: Record<
    string,
    {
      n_matured: number;
      n_winners: number;
      win_rate: number | null;
      median_excess: number | null;
      median_raw: number | null;
      mean_excess: number | null;
      damped_score: number | null;
      concentration: Concentration;
    }
  >;
  mentions: AccountMention[];
};

export type MentionResponse = {
  id: number;
  tweet_id: string;
  tweet_ts: string;
  tweet_text: string;
  raw_match: string | null;
  match_kind: string | null;
  sentiment: string | null;
  price_at_mention: number | null;
  account: { handle: string; display_name: string | null };
  token: {
    symbol: string | null;
    name: string | null;
    coingecko_id: string | null;
    contract_addr: string | null;
  };
  returns: Returns;
  matured: { "30d": boolean; "90d": boolean; "365d": boolean };
};

export type SeriesResponse = {
  mention_id: number;
  tweet_ts: string;
  p0: number | null;
  points: { ts: string; granularity: string; close_usd: number }[];
};

export type EquityCurvePoint = {
  ts: string;
  n: number;
  cum_mean: number;
  last_excess: number;
};

export type LeaderboardCurvesResponse = {
  cohort: Cohort;
  accounts: {
    account_id: number;
    handle: string;
    display_name: string | null;
    n_matured: number;
    median_excess: number | null;
    curve: EquityCurvePoint[];
  }[];
};

export type MentionCurvePoint = {
  day: number;
  excess: number;
  token_ret: number;
  btc_ret: number;
};

export type TokenChartsTokenMention = {
  handle: string;
  is_top: boolean;
  day: number;
  indexed: number | null;
  captured_ret: number | null;
  tweet_ts: string;
  // Tweet details for the hover-card. `oembed_html` is the cached X embed
  // markup (server-side cached via raw_tweets.oembed_html); `oembed_error`
  // non-null means X told us the tweet can't be embedded (deleted/private)
  // — render the plain `tweet_text` fallback in that case.
  tweet_id: string;
  tweet_text: string;
  oembed_html: string | null;
  oembed_error: string | null;
};

export type TokenChartsToken = {
  token_id: number;
  symbol: string | null;
  name: string | null;
  coingecko_id: string | null;
  t0_ts: string;
  p0: number;
  p_end: number;
  total_return: number;
  excess_return: number;
  series: { day: number; indexed: number }[];
  mentions: TokenChartsTokenMention[];
};

export type TokenChartsResponse = {
  cohort: "30d" | "90d";
  horizon_days: number;
  accounts: {
    account_id: number;
    handle: string;
    display_name: string | null;
    n_matured: number;
    median_excess: number | null;
  }[];
  tokens: TokenChartsToken[];
};

export type MentionCurvesResponse = {
  handle: string;
  cohort: Cohort;
  horizon_days: number;
  mentions: {
    id: number;
    tweet_ts: string;
    symbol: string | null;
    coingecko_id: string | null;
    final_excess: number | null;
    points: MentionCurvePoint[];
  }[];
};

export type BestCall = {
  mention_id: number;
  symbol: string | null;
  raw_ret: number | null;
  excess_ret: number | null;
  tweet_ts: string;
};

export type BestCallResponse = {
  handle: string;
  cohort: Cohort;
  best_call: BestCall | null;
};

async function fetchJson<T>(path: string, revalidate = 60): Promise<T> {
  const res = await fetch(`${BASE}/api${path}`, { next: { revalidate } });
  if (!res.ok) {
    throw new Error(`API ${path}: HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

export async function getLeaderboard(
  cohort: Cohort,
  sort: Sort,
  view: View = "scouts",
): Promise<{
  cohort: Cohort;
  sort: Sort;
  view: View;
  concentration_threshold: number;
  rows: LeaderboardRow[];
}> {
  return fetchJson(
    `/leaderboard?cohort=${cohort}&sort=${sort}&view=${view}&limit=200`,
  );
}

export async function getAccount(handle: string): Promise<AccountResponse> {
  return fetchJson(`/account/${encodeURIComponent(handle)}`);
}

export async function getMention(id: number): Promise<MentionResponse> {
  return fetchJson(`/mention/${id}`);
}

export async function getMentionSeries(id: number): Promise<SeriesResponse> {
  return fetchJson(`/mention/${id}/series`);
}

export async function getLeaderboardCurves(
  cohort: Cohort,
  limit = 10,
  view: View = "scouts",
): Promise<LeaderboardCurvesResponse> {
  return fetchJson(
    `/leaderboard/equity-curves?cohort=${cohort}&limit=${limit}&view=${view}`,
  );
}

export async function getTokenCharts(
  cohort: "30d" | "90d",
  limit = 9,
  accountsLimit = 10,
  view: View = "scouts",
): Promise<TokenChartsResponse> {
  return fetchJson(
    `/leaderboard/token-charts?cohort=${cohort}&limit=${limit}&accounts_limit=${accountsLimit}&view=${view}`,
  );
}

export async function getAccountMentionCurves(
  handle: string,
  cohort: Cohort,
): Promise<MentionCurvesResponse> {
  return fetchJson(
    `/account/${encodeURIComponent(handle)}/mention-curves?cohort=${cohort}&limit=120`,
  );
}

export async function getAccountBestCall(
  handle: string,
  cohort: Cohort,
): Promise<BestCallResponse> {
  return fetchJson(
    `/account/${encodeURIComponent(handle)}/best-call?cohort=${cohort}`,
  );
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = (v * 100).toFixed(digits);
  const n = Number(s);
  return `${n > 0 ? "+" : ""}${s}%`;
}

export function pctClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return "text-muted";
  if (v > 0) return "text-emerald-400";
  if (v < 0) return "text-rose-400";
  return "text-muted";
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toISOString().slice(0, 10);
}
