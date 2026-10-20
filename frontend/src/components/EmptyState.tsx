import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { ReactNode } from "react";

export function EmptyState({ title, description, action }: { title: ReactNode; description?: ReactNode; action?: ReactNode }) {
  return (
    <Box margin={{ vertical: "xs" }} textAlign="center" color="inherit">
      <SpaceBetween size="xxs">
        <Box variant="strong" color="inherit">
          {title}
        </Box>
        {description ? (
          <Box variant="p" color="inherit">
            {description}
          </Box>
        ) : null}
        {action ? <Box padding={{ top: "xs" }}>{action}</Box> : null}
      </SpaceBetween>
    </Box>
  );
}
