import Box from "@cloudscape-design/components/box";
import Spinner from "@cloudscape-design/components/spinner";
import type { ReactNode } from "react";

/** A stat tile: label, one large value, an optional secondary line (the dashboard's KPI row). */
export function KpiTile({
  label,
  value,
  hint,
  loading = false,
  children,
}: {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  loading?: boolean;
  children?: ReactNode;
}) {
  return (
    <div>
      <Box variant="awsui-key-label">{label}</Box>
      <Box variant="awsui-value-large" display="block" data-testid="kpi-value">
        {loading ? <Spinner size="normal" /> : value}
      </Box>
      {hint ? (
        <Box variant="small" color="text-body-secondary" display="block">
          {hint}
        </Box>
      ) : null}
      {children}
    </div>
  );
}
