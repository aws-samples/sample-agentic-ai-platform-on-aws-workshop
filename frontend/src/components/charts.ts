/**
 * Chart conventions for the dashboard (dataviz method): one y-axis per chart, categorical hues assigned to entities in a
 * fixed order (Cloudscape palette slots 1-4, validated with the dataviz validator on 9 Oct 2026: all checks pass, teal
 * carries a contrast warning that the legends and tables relieve), status colours only where the colour means good / bad.
 */
import type { Series } from "../api/types";

export const SERIES_COLORS = {
  slot1: "#688ae8", // blue
  slot2: "#c33d69", // magenta
  slot3: "#2ea597", // teal
  slot4: "#8456ce", // violet
} as const;

export const STATUS_COLORS = {
  positive: "#67a353",
  critical: "#ba2e0f",
  neutral: "#8c8c94",
} as const;

export const CATEGORICAL_ORDER: ReadonlyArray<string> = [
  SERIES_COLORS.slot1,
  SERIES_COLORS.slot2,
  SERIES_COLORS.slot3,
  SERIES_COLORS.slot4,
];

export interface TimePoint {
  x: Date;
  y: number;
}

export function toPoints(series: Series | undefined | null): TimePoint[] {
  if (!series) {
    return [];
  }
  return series
    .map(([timestamp, value]) => ({ x: new Date(timestamp), y: Number(value) }))
    .filter((point) => !Number.isNaN(point.x.getTime()) && Number.isFinite(point.y));
}

export function hasData(...series: Array<Series | undefined | null>): boolean {
  return series.some((s) => (s?.length ?? 0) > 0);
}

/** Tick labels: time of day inside a day, day + time across several days. */
export function makeTickFormatter(hours: number): (date: Date) => string {
  if (hours <= 24) {
    return (date) => date.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
  }
  return (date) =>
    date
      .toLocaleString("en-US", { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })
      .replace(",", "");
}

export function timeDomain(window: { start: string; end: string } | null | undefined, hours: number): [Date, Date] {
  if (window) {
    const start = new Date(window.start);
    const end = new Date(window.end);
    if (!Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime())) {
      return [start, end];
    }
  }
  const end = new Date();
  return [new Date(end.getTime() - hours * 3600_000), end];
}

export function sumSeries(series: Series | undefined | null): number {
  return (series ?? []).reduce((total, [, value]) => total + (Number(value) || 0), 0);
}

export function maxSeries(series: Series | undefined | null): number | null {
  const values = (series ?? []).map(([, value]) => Number(value)).filter(Number.isFinite);
  return values.length ? Math.max(...values) : null;
}

export function lastValue(series: Series | undefined | null): number | null {
  if (!series || !series.length) {
    return null;
  }
  const value = Number(series[series.length - 1][1]);
  return Number.isFinite(value) ? value : null;
}

/** Aggregation period (seconds) so that a window of `hours` has at most `maxBars` bars; a multiple of the API period. */
export function chartPeriod(hours: number, apiPeriod: number, maxBars = 48): number {
  const candidates = [60, 120, 300, 600, 900, 1800, 3600, 7200, 14_400, 21_600, 43_200, 86_400];
  const windowSeconds = hours * 3600;
  for (const candidate of candidates) {
    if (candidate >= apiPeriod && candidate % apiPeriod === 0 && windowSeconds / candidate <= maxBars) {
      return candidate;
    }
  }
  return candidates[candidates.length - 1];
}

/**
 * Re-bucket a series into `period`-second buckets spanning the window, with an explicit 0 for empty buckets, so every bar
 * has the same width and a single busy 5-minute bucket stays visible in a 24-hour chart. `sum` for counts, `max` for latency.
 */
export function rebucket(series: Series | undefined | null, domain: [Date, Date], period: number, mode: "sum" | "max" = "sum"): TimePoint[] {
  const periodMs = period * 1000;
  const start = Math.floor(domain[0].getTime() / periodMs) * periodMs;
  const end = domain[1].getTime();
  const buckets = new Map<number, number>();
  for (let t = start; t < end; t += periodMs) {
    buckets.set(t, 0);
  }
  for (const [timestamp, raw] of series ?? []) {
    const time = new Date(timestamp).getTime();
    const value = Number(raw);
    if (Number.isNaN(time) || !Number.isFinite(value)) {
      continue;
    }
    const bucket = Math.floor(time / periodMs) * periodMs;
    if (!buckets.has(bucket)) {
      continue;
    }
    const current = buckets.get(bucket) ?? 0;
    buckets.set(bucket, mode === "sum" ? current + value : Math.max(current, value));
  }
  return Array.from(buckets.entries())
    .sort(([a], [b]) => a - b)
    .map(([time, value]) => ({ x: new Date(time), y: Math.round(value * 100) / 100 }));
}

export function periodLabel(period: number): string {
  if (period >= 86_400) {
    return `${period / 86_400} d`;
  }
  if (period >= 3600) {
    return `${period / 3600} h`;
  }
  return `${period / 60} min`;
}

export interface CategoryPoint {
  x: string;
  y: number;
}

/**
 * Cloudscape draws bars only on a categorical x axis (a time scale has no band width: "Bar series cannot be used with a
 * time scale"), so bar charts use the bucket start as an ISO string category; `categoryTickFormatter` prints it as a time.
 */
export function toCategories(points: TimePoint[]): CategoryPoint[] {
  return points.map((point) => ({ x: point.x.toISOString(), y: point.y }));
}

export function bucketCategories(domain: [Date, Date], period: number): string[] {
  return rebucket(undefined, domain, period).map((point) => point.x.toISOString());
}

export function categoryTickFormatter(hours: number): (category: string) => string {
  const format = makeTickFormatter(hours);
  return (category) => {
    const date = new Date(category);
    return Number.isNaN(date.getTime()) ? category : format(date);
  };
}
