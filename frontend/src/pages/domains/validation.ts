/** Client-side mirrors of backend/src/platform_api/validation.py (the API remains the authority). */
export const ID_RE = /^[a-z][a-z0-9-]{2,30}$/;
export const BUDGET_DURATION_RE = /^\d+(s|m|h|d|mo)$/;

export function idError(value: string, what = "id"): string | undefined {
  if (!value.trim()) {
    return `${what} is required`;
  }
  if (!ID_RE.test(value.trim())) {
    return "kebab-case: start with a letter, 3-31 characters of a-z, 0-9 and - (example: hr-assistant)";
  }
  return undefined;
}

export function numberError(value: string, { minimum, integer, required = true }: { minimum?: number; integer?: boolean; required?: boolean }): string | undefined {
  if (!value.trim()) {
    return required ? "required" : undefined;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return "must be a number";
  }
  if (integer && !Number.isInteger(parsed)) {
    return "must be an integer";
  }
  if (minimum !== undefined && parsed < minimum) {
    return `must be >= ${minimum}`;
  }
  return undefined;
}

export function durationError(value: string): string | undefined {
  if (!value.trim()) {
    return "required";
  }
  if (!BUDGET_DURATION_RE.test(value.trim())) {
    return "like 30s, 30m, 24h, 7d or 1mo (LiteLLM budget_duration)";
  }
  return undefined;
}

/** The path named by a nested validation error ("defaultLimits.maxTokens must be ..."), if any. */
export function nestedErrorPath(message: string): { section: string; field: string } | null {
  const match = /\b(defaultLimits|defaultProjectCap|defaultKeyCap)\.([A-Za-z]+)/.exec(message);
  return match ? { section: match[1], field: match[2] } : null;
}
