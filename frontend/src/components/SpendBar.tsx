import ProgressBar from "@cloudscape-design/components/progress-bar";

import { formatDateTime, formatRelative, formatUsd, spendPercent, toNumber } from "../utils/format";

/** Spend against a LiteLLM cap (team or key) as a meter with the reset time. */
export function SpendBar({
  spend,
  cap,
  resetAt,
  blocked,
  label,
}: {
  spend: unknown;
  cap: unknown;
  resetAt?: string | null;
  blocked?: boolean | null;
  label?: string;
}) {
  const percent = spendPercent(spend, cap);
  const used = toNumber(spend);
  const limit = toNumber(cap);
  const reached = blocked || (percent !== null && percent >= 100);
  const description = `${formatUsd(used ?? 0)} of ${limit !== null ? formatUsd(limit) : "no cap"}`;
  const additional = resetAt
    ? `Resets ${formatRelative(resetAt)} (${formatDateTime(resetAt)})`
    : limit === null
      ? "No cap configured"
      : undefined;
  if (reached) {
    return (
      <ProgressBar
        value={100}
        status="error"
        label={label}
        resultText={`${description}: cap reached${blocked ? ", key blocked by LiteLLM" : ""}`}
        additionalInfo={additional}
      />
    );
  }
  return <ProgressBar value={percent ?? 0} label={label} description={description} additionalInfo={additional} />;
}
