/**
 * Which vendor a model belongs to, judged from its alias or model id. This is the one list to edit when a new family
 * arrives (add a `match` token) or a new vendor appears (add an entry here and its mark in components/VendorIcon.tsx).
 * Matching is case-insensitive and runs in order, so put the more specific tokens first.
 */
export type ModelVendor = "openai" | "anthropic" | "aws";

export interface ModelVendorRule {
  vendor: ModelVendor;
  /** Shown as the icon's accessible name. */
  label: string;
  /** Substrings of the alias or model id that identify the vendor's models. */
  match: readonly string[];
}

export const MODEL_VENDOR_RULES: readonly ModelVendorRule[] = [
  { vendor: "openai", label: "OpenAI", match: ["gpt"] },
  { vendor: "anthropic", label: "Anthropic", match: ["claude"] },
  { vendor: "aws", label: "AWS", match: ["nova"] },
];

/** The vendor rule for a model, or null when neither the alias nor the model id names a known family. */
export function vendorForModel(...names: Array<string | null | undefined>): ModelVendorRule | null {
  const haystack = names
    .filter((n): n is string => typeof n === "string" && n.length > 0)
    .join(" ")
    .toLowerCase();
  if (!haystack) return null;
  return MODEL_VENDOR_RULES.find((rule) => rule.match.some((token) => haystack.includes(token.toLowerCase()))) ?? null;
}
