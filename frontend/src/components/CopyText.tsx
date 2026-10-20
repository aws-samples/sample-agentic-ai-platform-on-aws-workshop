import Box from "@cloudscape-design/components/box";
import CopyToClipboard from "@cloudscape-design/components/copy-to-clipboard";

import { truncate } from "../utils/format";

/** A value (ARN, id, URL) with a copy button; long values are shortened in the display only. */
export function CopyText({ value, max = 64 }: { value: string | null | undefined; max?: number }) {
  if (!value) {
    return <Box color="text-body-secondary">—</Box>;
  }
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, maxWidth: "100%", minWidth: 0, whiteSpace: "normal" }}>
      <span title={value} style={{ minWidth: 0, overflowWrap: "anywhere" }}>
        {truncate(value, max)}
      </span>
      <CopyToClipboard
        variant="icon"
        textToCopy={value}
        copyButtonAriaLabel={`Copy ${truncate(value, 40)}`}
        copySuccessText="Copied"
        copyErrorText="Could not copy"
      />
    </span>
  );
}
