"use client";

import { useEffect, useRef, useState } from "react";

import { hydrateTweets } from "@/lib/twitter-widgets";

type TweetOEmbedResponse = {
  tweet_id: string;
  html: string | null;
  error: string | null;
};

type Props = {
  tweetId: string;
  handle: string;
  // Cached oEmbed HTML from the chart payload (raw_tweets.oembed_html).
  // null means not yet cached → we'll lazy-fetch on mount.
  oembedHtml: string | null;
  // Non-null means X said this tweet can't be embedded (deleted/private/
  // forbidden). Skip the iframe attempt and stay on plain-text fallback.
  oembedError: string | null;
  // Plain text fallback shown immediately on mount, replaced by the
  // styled X iframe once widgets.js hydrates.
  tweetText: string;
  tweetTs: string;
};

function fmtTweetTs(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function TweetEmbedCard({
  tweetId,
  handle,
  oembedHtml,
  oembedError,
  tweetText,
  tweetTs,
}: Props) {
  const embedHostRef = useRef<HTMLDivElement>(null);
  const [html, setHtml] = useState<string | null>(oembedHtml);
  const [terminalError, setTerminalError] = useState<string | null>(oembedError);
  const [hydrated, setHydrated] = useState(false);

  // Lazy-fetch when the chart payload didn't ship cached HTML.
  useEffect(() => {
    if (html || terminalError) return;
    let cancelled = false;
    fetch(`/api/tweet/${encodeURIComponent(tweetId)}/oembed`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: TweetOEmbedResponse) => {
        if (cancelled) return;
        if (data.error) setTerminalError(data.error);
        if (data.html) setHtml(data.html);
      })
      .catch(() => {
        // Transient — leave the plain-text fallback in place. Next hover
        // will retry naturally.
      });
    return () => {
      cancelled = true;
    };
  }, [tweetId, html, terminalError]);

  // Inject + hydrate the blockquote whenever the html changes.
  useEffect(() => {
    const host = embedHostRef.current;
    if (!host || !html) return;
    host.innerHTML = html;
    setHydrated(false);
    hydrateTweets(host)
      .then(() => setHydrated(true))
      .catch(() => {
        // widgets.js failed → leave the raw blockquote rendered; it's
        // still readable, just unstyled.
      });
  }, [html]);

  if (terminalError) {
    return (
      <FallbackCard
        handle={handle}
        tweetId={tweetId}
        tweetText={tweetText}
        tweetTs={tweetTs}
        reason={terminalError}
      />
    );
  }

  return (
    <div className="relative">
      {/* Plain-text fallback shown until widgets.js renders the iframe.
          Once hydrated, the iframe overlays; we hide the fallback. */}
      {!hydrated ? (
        <FallbackCard
          handle={handle}
          tweetId={tweetId}
          tweetText={tweetText}
          tweetTs={tweetTs}
          reason={null}
          minimal
        />
      ) : null}
      <div
        ref={embedHostRef}
        // X's iframe is fixed-width 550px max — we constrain the parent so it
        // resizes the iframe down. theme=dark via the oEmbed request.
        className="[&_.twitter-tweet]:!my-0 [&_.twitter-tweet]:!mx-0 [&_iframe]:!max-w-full"
        style={{ minHeight: hydrated ? undefined : 0 }}
      />
    </div>
  );
}

function FallbackCard({
  handle,
  tweetId,
  tweetText,
  tweetTs,
  reason,
  minimal = false,
}: {
  handle: string;
  tweetId: string;
  tweetText: string;
  tweetTs: string;
  reason: string | null;
  minimal?: boolean;
}) {
  const truncated =
    tweetText.length > 280 ? tweetText.slice(0, 277) + "…" : tweetText;
  return (
    <div
      className={
        minimal
          ? "rounded-lg border border-white/10 bg-bg/95 p-3 text-[12px] leading-snug text-ink"
          : "rounded-lg border border-white/10 bg-surface p-3 text-[12px] leading-snug text-ink"
      }
    >
      <div className="mb-1 flex items-baseline justify-between gap-2 text-[10px] text-muted">
        <a
          href={`https://x.com/${handle}/status/${tweetId}`}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-ink hover:text-accent hover:underline"
        >
          @{handle}
        </a>
        <span>{fmtTweetTs(tweetTs)}</span>
      </div>
      <div className="whitespace-pre-wrap break-words">{truncated}</div>
      <div className="mt-2 flex items-center justify-between gap-2 text-[10px] text-muted">
        <a
          href={`https://x.com/${handle}/status/${tweetId}`}
          target="_blank"
          rel="noreferrer"
          className="text-accent hover:underline"
        >
          View on X →
        </a>
        {reason ? (
          <span title={`oembed: ${reason}`} className="italic">
            {reason === "not_found"
              ? "tweet unavailable"
              : reason === "forbidden" || reason === "unauthorized"
                ? "protected"
                : "embed unavailable"}
          </span>
        ) : null}
      </div>
    </div>
  );
}
