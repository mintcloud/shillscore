// Shared chart palette. Picked for maximum perceptual separation on a dark
// background — both hue AND brightness spaced. The first three are placed at
// opposite ends of the color wheel so leaderboards with only 2–3 accounts
// (the common case for thin token panels) never collide on shades of blue.
export const CHART_PALETTE: readonly string[] = [
  "#ef4444", // red
  "#22d3ee", // cyan
  "#fbbf24", // amber
  "#a78bfa", // violet
  "#34d399", // emerald
  "#f472b6", // pink
  "#3b82f6", // blue
  "#fb923c", // orange
  "#a3e635", // lime
  "#e879f9", // fuchsia
  "#14b8a6", // teal
  "#facc15", // yellow
];

export function colorFor(i: number): string {
  return CHART_PALETTE[i % CHART_PALETTE.length];
}
