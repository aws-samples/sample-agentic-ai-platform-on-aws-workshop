import Box from "@cloudscape-design/components/box";
import CopyToClipboard from "@cloudscape-design/components/copy-to-clipboard";
import SpaceBetween from "@cloudscape-design/components/space-between";
import * as tokens from "@cloudscape-design/design-tokens";

/** Pretty-printed JSON (or text) in a scrollable code block with a copy button. */
export function JsonBlock({ value, maxHeight = 420, label = "JSON" }: { value: unknown; maxHeight?: number; label?: string }) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <SpaceBetween size="xs">
      <Box float="right">
        <CopyToClipboard
          variant="button"
          copyButtonText={`Copy ${label}`}
          copySuccessText={`${label} copied`}
          copyErrorText={`${label} could not be copied`}
          textToCopy={text}
        />
      </Box>
      <pre
        style={{
          margin: 0,
          padding: tokens.spaceScaledS,
          maxHeight,
          overflow: "auto",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          fontFamily: tokens.fontFamilyMonospace,
          fontSize: tokens.fontSizeBodyS,
          lineHeight: tokens.lineHeightBodyS,
          background: tokens.colorBackgroundLayoutMain,
          border: `1px solid ${tokens.colorBorderDividerDefault}`,
          borderRadius: tokens.borderRadiusContainer,
          color: tokens.colorTextBodyDefault,
        }}
      >
        {text}
      </pre>
    </SpaceBetween>
  );
}
