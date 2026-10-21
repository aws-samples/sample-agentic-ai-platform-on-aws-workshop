import type { CSSProperties } from "react";

import type { ModelVendor } from "../config/modelVendors";
import { VENDOR_MARKS } from "./vendorMarks";

/** Default icon size in px: Cloudscape's small icon, so the mark lines up with the 14px body text next to it. */
export const VENDOR_ICON_SIZE = 16;

export function VendorIcon({
  vendor,
  size = VENDOR_ICON_SIZE,
  color = "currentColor",
  title,
  style,
}: {
  vendor: ModelVendor;
  size?: number;
  color?: string;
  /** Accessible name; omit when the vendor is already named in adjacent text and the mark is decorative. */
  title?: string;
  style?: CSSProperties;
}) {
  const mark = VENDOR_MARKS[vendor];
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      focusable="false"
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
      style={{ display: "inline-block", verticalAlign: "text-bottom", flex: "none", ...style }}
    >
      {title ? <title>{title}</title> : null}
      <path fill={color} stroke="none" d={mark.path} />
    </svg>
  );
}

/** A fixed-width blank that keeps aliases without a known vendor aligned with the ones that have a mark. */
export function VendorIconSpacer({ size = VENDOR_ICON_SIZE }: { size?: number }) {
  return <span aria-hidden="true" style={{ display: "inline-block", width: size, height: size, flex: "none" }} />;
}
