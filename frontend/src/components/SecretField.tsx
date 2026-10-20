import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import CopyToClipboard from "@cloudscape-design/components/copy-to-clipboard";
import Input from "@cloudscape-design/components/input";
import Spinner from "@cloudscape-design/components/spinner";
import { useState } from "react";

// Cloudscape has no eye icon of its own: 16x16 strokes in the style of its icon set (viewBox 0 0 16 16, 2px strokes).
const EYE = (
  <svg viewBox="0 0 16 16" focusable="false" aria-hidden="true">
    <path d="M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
    <circle cx="8" cy="8" r="2" fill="none" stroke="currentColor" strokeWidth="2" />
  </svg>
);
const EYE_OFF = (
  <svg viewBox="0 0 16 16" focusable="false" aria-hidden="true">
    <path d="M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
    <circle cx="8" cy="8" r="2" fill="none" stroke="currentColor" strokeWidth="2" />
    <path d="M2.5 2.5l11 11" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
  </svg>
);

/**
 * A secret (password, key) in a read-only password field: masked by default, revealed with the eye button, copied with
 * the copy button without revealing it. `label` names the secret for assistive technology ("LiteLLM admin password").
 */
export function SecretField({
  value,
  label,
  loading = false,
  error,
}: {
  value: string | null | undefined;
  label: string;
  loading?: boolean;
  error?: string | null;
}) {
  const [visible, setVisible] = useState(false);
  if (loading) {
    return (
      <Box color="text-body-secondary">
        <Spinner /> Loading…
      </Box>
    );
  }
  if (error) {
    return <Box color="text-status-error">{error}</Box>;
  }
  if (!value) {
    return <Box color="text-body-secondary">—</Box>;
  }
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4, flexWrap: "nowrap" }}>
      <div style={{ flex: "1 1 160px", minWidth: 0, maxWidth: 420 }}>
        <Input
          value={value}
          type={visible ? "text" : "password"}
          readOnly
          autoComplete={false}
          spellcheck={false}
          disableBrowserAutocorrect
          ariaLabel={label}
          data-testid="secret-input"
        />
      </div>
      <Button
        variant="icon"
        iconSvg={visible ? EYE_OFF : EYE}
        ariaLabel={visible ? `Hide ${label}` : `Show ${label}`}
        onClick={() => setVisible((current) => !current)}
        data-testid="secret-toggle"
      />
      <CopyToClipboard variant="icon" textToCopy={value} copyButtonAriaLabel={`Copy ${label}`} copySuccessText="Copied" copyErrorText="Could not copy" />
    </div>
  );
}
