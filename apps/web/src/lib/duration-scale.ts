// One fixed scale across runs, themes and filters so colors remain comparable.
export const durationBands = [
  { minMs: 0, label: '< 5 秒' },
  { minMs: 5_000, label: '5–15 秒' },
  { minMs: 15_000, label: '15–30 秒' },
  { minMs: 30_000, label: '30–60 秒' },
  { minMs: 60_000, label: '1–2 分钟' },
  { minMs: 120_000, label: '≥ 2 分钟' },
] as const;

export function durationLevel(ms: number | null | undefined): number | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  return durationBands.filter(band => ms >= band.minMs).length - 1;
}
