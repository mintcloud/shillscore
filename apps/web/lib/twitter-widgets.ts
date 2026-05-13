// platform.twitter.com/widgets.js loader — load once per page, hydrate
// any number of blockquotes via twttr.widgets.load(el).
//
// Free, unauthenticated, CDN-cached. NOT the paid X API v2 — this is the
// embed ecosystem JS that every news site uses. See:
//   https://developer.x.com/en/docs/twitter-for-websites/javascript-api
//
// We pre-load eagerly on `idle` after first interaction so the *first*
// hover doesn't pay the ~30KB script cost. Subsequent hovers just call
// twttr.widgets.load() on the new DOM nodes.

declare global {
  interface Window {
    twttr?: {
      widgets?: {
        load: (el?: HTMLElement) => Promise<void>;
      };
      ready?: (fn: (twttr: Window["twttr"]) => void) => void;
    };
  }
}

const WIDGETS_SRC = "https://platform.twitter.com/widgets.js";

let loadPromise: Promise<void> | null = null;

export function loadTwitterWidgets(): Promise<void> {
  if (typeof window === "undefined") return Promise.resolve();
  if (window.twttr?.widgets) return Promise.resolve();
  if (loadPromise) return loadPromise;

  loadPromise = new Promise<void>((resolve, reject) => {
    // Reuse an existing script tag if something else loaded it.
    const existing = document.querySelector<HTMLScriptElement>(
      `script[src="${WIDGETS_SRC}"]`,
    );
    if (existing) {
      existing.addEventListener("load", () => resolve());
      existing.addEventListener("error", () => reject(new Error("widgets.js load failed")));
      return;
    }
    const s = document.createElement("script");
    s.src = WIDGETS_SRC;
    s.async = true;
    s.charset = "utf-8";
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("widgets.js load failed"));
    document.head.appendChild(s);
  });
  return loadPromise;
}

/** Render any twitter-tweet blockquotes inside `el` as styled iframes. */
export async function hydrateTweets(el: HTMLElement): Promise<void> {
  await loadTwitterWidgets();
  await window.twttr?.widgets?.load?.(el);
}
