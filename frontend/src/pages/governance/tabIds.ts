/** The tabs of the Governance screen's approved inventory; the active one travels in the URL as ?tab=. */
export const APPROVED_TAB_IDS = ["models", "knowledge-bases", "tools"] as const;
export type ApprovedTabId = (typeof APPROVED_TAB_IDS)[number];

export function isApprovedTab(value: string | null | undefined): value is ApprovedTabId {
  return (APPROVED_TAB_IDS as ReadonlyArray<string>).includes(value ?? "");
}
