# What is `user_follows` for?

Short answer: it's the **only** per-user data we keep. Two jobs:

## 1. Per-user filtering of the global leaderboard

The `accounts` and `mentions` tables are global (a public good — that's the whole network-effect point). The leaderboard at `/` shows every account anyone has ever brought in.

But each user also wants *their own* view: "rank only the accounts **I** follow." That's the `/me` page. Implementation:

```sql
SELECT a.handle, score, n_mentions
FROM leaderboard_mv lb
JOIN accounts a ON a.id = lb.account_id
JOIN user_follows uf ON uf.account_id = a.id
WHERE uf.user_id = $current_user
ORDER BY score DESC;
```

Without `user_follows`, we have no way to filter the global table down to a user's feed.

## 2. Driving the sync scheduler

When a user connects, we need to know which accounts to enqueue for diff-fetch. The diff logic in §4 of the plan is:

```
follows = twitter.get_follows(user_id)         # from Twitter API
existing = SELECT account_id FROM user_follows WHERE user_id = $u
new_links = follows - existing
INSERT INTO user_follows (user_id, account_id) ...

for handle in follows:
    if account.last_synced_at < now - 24h:
        enqueue sync_account(handle, since_id=account.last_tweet_id)
```

`user_follows` is what lets us answer "what accounts does user X care about?" *without* re-hitting Twitter every time. We hit Twitter once at connect, and at periodic re-syncs to detect new follows.

## Why not store follows on `users` as a JSON array?

Could, but:
- We want indexed joins (`WHERE user_id = X` and `WHERE account_id = Y`).
- `account_id = Y` matters for the inverse query: "how many of our users follow this account?" — useful later for popularity-weighting and for deciding which accounts to refresh first.
- A junction table is the boring correct answer; a JSON array would just become a junction table with extra steps.

## Privacy note

Per §8 of the plan: `user_follows` rows are **never exposed publicly**. We don't show "user X follows account Y" anywhere. The data exists only to (a) filter the user's own view and (b) drive the sync queue. If a user disconnects, we delete their `user_follows` rows; the global `accounts`/`mentions` data stays (it was never theirs to begin with).
