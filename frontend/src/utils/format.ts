/** Formatting helpers shared by the screens (money, token prices, durations, dates, counts). */

const EM_DASH = "—";

export function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function toNumber(value: unknown): number | null {
  if (isFiniteNumber(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/** USD amounts: two decimals, four for sub-cent spend (LiteLLM reports 0.00092 USD per call). */
export function formatUsd(value: unknown): string {
  const amount = toNumber(value);
  if (amount === null) {
    return EM_DASH;
  }
  if (amount === 0) {
    return "$0.00";
  }
  const abs = Math.abs(amount);
  const digits = abs < 0.01 ? 4 : 2;
  return `${amount < 0 ? "-" : ""}$${abs.toFixed(digits)}`;
}

/** LiteLLM prices are USD per token; people read USD per million tokens. */
export function formatPricePerMillion(perToken: unknown): string {
  const price = toNumber(perToken);
  if (price === null) {
    return EM_DASH;
  }
  const perMillion = price * 1_000_000;
  return `$${perMillion.toFixed(perMillion >= 10 ? 1 : 2)} / 1M tokens`;
}

export function formatNumber(value: unknown, maximumFractionDigits = 0): string {
  const amount = toNumber(value);
  if (amount === null) {
    return EM_DASH;
  }
  return amount.toLocaleString("en-US", { maximumFractionDigits });
}

/** Compact counts for KPI tiles: 1234 -> 1.2k, 1500000 -> 1.5M. */
export function formatCompact(value: unknown): string {
  const amount = toNumber(value);
  if (amount === null) {
    return EM_DASH;
  }
  const abs = Math.abs(amount);
  if (abs >= 1_000_000) {
    return `${(amount / 1_000_000).toFixed(1)}M`;
  }
  if (abs >= 10_000) {
    return `${(amount / 1_000).toFixed(0)}k`;
  }
  if (abs >= 1_000) {
    return `${(amount / 1_000).toFixed(1)}k`;
  }
  return Number.isInteger(amount) ? String(amount) : amount.toFixed(1);
}

export function formatPercent(ratio: unknown, digits = 1): string {
  const value = toNumber(ratio);
  if (value === null) {
    return EM_DASH;
  }
  return `${(value * 100).toFixed(digits)}%`;
}

/** Milliseconds as a short latency: 320 ms, 1.5 s, 31.8 s, 2 min 5 s. */
export function formatMillis(value: unknown): string {
  const ms = toNumber(value);
  if (ms === null) {
    return EM_DASH;
  }
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  return formatSeconds(ms / 1000);
}

/** Seconds as a short duration: 31.8 s, 2 min 5 s, 1 h 2 min, 3 d 4 h. */
export function formatSeconds(value: unknown): string {
  const seconds = toNumber(value);
  if (seconds === null) {
    return EM_DASH;
  }
  if (seconds < 60) {
    return `${Number.isInteger(seconds) ? seconds : seconds.toFixed(1)} s`;
  }
  const whole = Math.round(seconds);
  const days = Math.floor(whole / 86_400);
  const hours = Math.floor((whole % 86_400) / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const rest = whole % 60;
  if (days > 0) {
    return hours ? `${days} d ${hours} h` : `${days} d`;
  }
  if (hours > 0) {
    return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
  }
  return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
}

const DURATION_UNITS: Record<string, [string, string]> = {
  s: ["second", "seconds"],
  m: ["minute", "minutes"],
  h: ["hour", "hours"],
  d: ["day", "days"],
  mo: ["month", "months"],
};

/** LiteLLM budget durations ("7d", "24h", "1mo") as words; unknown strings are returned unchanged. */
export function formatBudgetDuration(value: unknown): string {
  if (typeof value !== "string" || !value) {
    return EM_DASH;
  }
  const match = /^(\d+)(s|m|h|d|mo)$/.exec(value.trim());
  if (!match) {
    return value;
  }
  const amount = Number(match[1]);
  const [singular, plural] = DURATION_UNITS[match[2]];
  return `${amount} ${amount === 1 ? singular : plural}`;
}

export function parseDate(value: unknown): Date | null {
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value;
  }
  if (typeof value !== "string" || !value) {
    return null;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * ISO timestamps (the API uses `...Z` or `+00:00`) as a readable local date and time with the zone name, so a LiteLLM
 * UTC-midnight budget reset shown to a US-Eastern attendee ("Oct 11, 2026, 20:00 EDT") can be matched with the UTC output of the workshop command.
 */
export function formatDateTime(value: unknown): string {
  const date = parseDate(value);
  if (!date) {
    return EM_DASH;
  }
  return date.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZoneName: "short",
  });
}

export function formatTime(value: unknown): string {
  const date = parseDate(value);
  return date ? date.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short" }) : EM_DASH;
}

/** "in 3 days", "2 h ago" relative to `now` (default: the current time). */
export function formatRelative(value: unknown, now: Date = new Date()): string {
  const date = parseDate(value);
  if (!date) {
    return EM_DASH;
  }
  const diffSeconds = Math.round((date.getTime() - now.getTime()) / 1000);
  const abs = Math.abs(diffSeconds);
  const future = diffSeconds > 0;
  let text: string;
  if (abs < 45) {
    text = "moments";
  } else if (abs < 3600) {
    text = `${Math.round(abs / 60)} min`;
  } else if (abs < 86_400) {
    text = `${Math.round(abs / 3600)} h`;
  } else {
    const days = Math.round(abs / 86_400);
    text = `${days} ${days === 1 ? "day" : "days"}`;
  }
  return future ? `in ${text}` : `${text} ago`;
}

/** The last path segment of an ARN or URL (gateway id, policy id, role name). */
export function shortId(value: unknown): string {
  if (typeof value !== "string" || !value) {
    return EM_DASH;
  }
  const parts = value.split(/[/:]/).filter(Boolean);
  return parts[parts.length - 1] ?? value;
}

export function truncate(value: unknown, max = 80): string {
  if (typeof value !== "string") {
    return value == null ? "" : String(value);
  }
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

/** Ratio for progress bars: 0-100, capped, null when the cap is unknown or zero. */
export function spendPercent(spend: unknown, cap: unknown): number | null {
  const used = toNumber(spend);
  const limit = toNumber(cap);
  if (used === null || limit === null || limit <= 0) {
    return null;
  }
  return Math.min(100, Math.max(0, (used / limit) * 100));
}
