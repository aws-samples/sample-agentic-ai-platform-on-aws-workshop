import Button from "@cloudscape-design/components/button";
import FormField, { type FormFieldProps } from "@cloudscape-design/components/form-field";
import Header, { type HeaderProps } from "@cloudscape-design/components/header";
import Popover from "@cloudscape-design/components/popover";
import Tooltip from "@cloudscape-design/components/tooltip";
import { type CSSProperties, type ReactNode, useEffect, useRef, useState } from "react";

/**
 * The guidance that used to sit under a heading or a form label as a `description`, moved behind a round (i) icon.
 *
 * Hovering the icon shows the text as a tooltip; clicking it (or Enter / Space on the focused icon) pins the same text in a
 * popover that closes on an outside click, Escape or Tab. The icon sits right after the heading / label (Cloudscape's `info`
 * slot), and the slot the description occupied keeps its height through `INFO_SPACER` (a non-breaking space in the description
 * position) while the text itself stays there for screen readers only, so form controls keep their accessible description.
 * The page gets quieter; the layout does not move.
 */
export function InfoPopover({ label, content, position = "bottom" }: { label: string; content: ReactNode; position?: "top" | "bottom" | "left" | "right" }) {
  const trackRef = useRef<HTMLSpanElement>(null);
  const [hovered, setHovered] = useState(false);
  // Cloudscape's Popover is uncontrolled and renders its content only while open: a sensor inside the content reports the
  // exact open state (outside click, Escape, Tab all close it), so the tooltip never shows beside a pinned popover.
  const [open, setOpen] = useState(false);
  return (
    <span
      ref={trackRef}
      className="info-popover"
      style={{ display: "inline-flex", verticalAlign: "middle" }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      data-testid="info-popover"
    >
      <Popover
        triggerType="custom"
        dismissButton={false}
        position={position}
        size="medium"
        content={<OpenSensor onChange={setOpen}>{content}</OpenSensor>}
        renderWithPortal
      >
        <Button
          variant="inline-icon"
          iconName="status-info"
          ariaLabel={`Information about ${label}`}
          ariaExpanded={open}
          // Cloudscape copies the aria-label into the native `title`, which would add the browser's own tooltip on top of ours;
          // the label stays for assistive technology (one dev-only Cloudscape warning about the override is expected).
          nativeButtonAttributes={{ title: undefined }}
        />
      </Popover>
      {hovered && !open ? <Tooltip getTrack={() => trackRef.current} content={content} position={position} onEscape={() => setHovered(false)} /> : null}
    </span>
  );
}

function OpenSensor({ onChange, children }: { onChange: (open: boolean) => void; children: ReactNode }) {
  useEffect(() => {
    onChange(true);
    return () => onChange(false);
  }, [onChange]);
  return <span>{children}</span>;
}

/** A non-breaking space keeps the description slot's height once its text has moved into the (i) icon. */
export const INFO_SPACER = " ";

/** Visually hidden, read by screen readers: the moved text stays the control's accessible description. */
const SCREEN_READER_ONLY: CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  margin: -1,
  padding: 0,
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap",
  border: 0,
};

function hiddenHelp(help: ReactNode): ReactNode {
  return (
    <>
      <span style={SCREEN_READER_ONLY}>{help}</span>
      {INFO_SPACER}
    </>
  );
}

function labelOf(children: ReactNode, label: string | undefined, fallback: string): string {
  if (label) {
    return label;
  }
  return typeof children === "string" || typeof children === "number" ? String(children) : fallback;
}

/**
 * `<Header variant="h2">` whose guidance lives behind the (i) icon. `help` is the former description; `description` stays
 * available for values that must remain visible (a model name, a period, a timestamp) and defaults to the spacer with the help
 * text hidden inside it for screen readers.
 */
export function InfoHeader({ help, label, description, children, ...props }: Omit<HeaderProps, "variant" | "info"> & { help: ReactNode; label?: string }) {
  return (
    <Header variant="h2" {...props} info={<InfoPopover label={labelOf(children, label, "this section")} content={help} />} description={description ?? hiddenHelp(help)}>
      {children}
    </Header>
  );
}

/** `<FormField>` whose guidance lives behind the (i) icon next to the label; the description slot keeps its height and its
 * aria-describedby wiring (the help text stays there, visually hidden). */
export function InfoFormField({ help, label, description, children, ...props }: Omit<FormFieldProps, "info"> & { help: ReactNode }) {
  return (
    <FormField {...props} label={label} info={<InfoPopover label={labelOf(label, undefined, "this field")} content={help} />} description={description ?? hiddenHelp(help)}>
      {children}
    </FormField>
  );
}
