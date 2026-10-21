import type { CatalogueCategory, CatalogueRecord } from "../../api/types";

/** The Catalogue's Records tabs: every record, then one tab per category the backend assigns (`category` on each record). */
export const CATALOGUE_TABS = [
  { id: "all", label: "All" },
  { id: "model", label: "Models" },
  { id: "knowledge-base", label: "Knowledge bases" },
  { id: "tool", label: "Tools" },
  { id: "agent", label: "Agents" },
] as const;
export type CatalogueTabId = (typeof CATALOGUE_TABS)[number]["id"];

const CATEGORY_LABELS: Record<CatalogueCategory, string> = {
  model: "Model",
  "knowledge-base": "Knowledge base",
  tool: "Tool",
  agent: "Agent",
  other: "Other",
};

export function isCatalogueTab(value: string | null | undefined): value is CatalogueTabId {
  return CATALOGUE_TABS.some((tab) => tab.id === value);
}

/** A record's category. The backend sets it; without one (an older API) the record type still places agents and MCP tool
 * servers, while a CUSTOM record needs the backend's reading of its descriptor and lands under All only. */
export function categoryOf(record: Pick<CatalogueRecord, "category" | "recordType">): CatalogueCategory {
  if (record.category) {
    return record.category;
  }
  return record.recordType === "AGENT" ? "agent" : record.recordType === "MCP" || record.recordType === "GATEWAY" ? "tool" : "other";
}

export function categoryLabel(category: CatalogueCategory | null | undefined): string {
  return category ? (CATEGORY_LABELS[category] ?? category) : "—";
}

type Categorised = Pick<CatalogueRecord, "category" | "recordType">;

/** The rows of one tab: everything under All, one category elsewhere (records of an unknown kind show under All only). */
export function rowsForTab<T extends Categorised>(rows: T[], tab: CatalogueTabId): T[] {
  return tab === "all" ? rows : rows.filter((row) => categoryOf(row) === tab);
}

/** "Models (2)": the tab label with the count of its rows, so the split is readable before switching. */
export function tabLabel(tab: (typeof CATALOGUE_TABS)[number], rows: ReadonlyArray<Categorised> | undefined): string {
  return rows ? `${tab.label} (${rowsForTab([...rows], tab.id).length})` : tab.label;
}

function tabNoun(tab: CatalogueTabId): string {
  return tab === "all" ? "records" : (CATALOGUE_TABS.find((t) => t.id === tab)?.label.toLowerCase() ?? "records");
}

/** The title of an empty tab ("No records" on All, which the e2e expects; "No approved models match" during a search). */
export function emptyTitle(tab: CatalogueTabId, searching: boolean): string {
  if (searching) {
    return tab === "all" ? "No approved records match" : `No approved ${tabNoun(tab)} match`;
  }
  return `No ${tabNoun(tab)}`;
}

/** What an empty tab means (the Agents tab is filled by the promote job, the others by module 4 and the registry itself). */
export function emptyDescription(tab: CatalogueTabId, status: string, searching: boolean): string {
  if (searching) {
    return (
      "Search covers discoverable (APPROVED) records only; clear the search to list every record by status." +
      (tab === "all" ? "" : " The All tab shows every hit of this search.")
    );
  }
  if (tab === "agent") {
    return "An agent appears here once a domain approver promotes its release to Prod: the promote job registers it as an AGENT record and approves it. Agents promoted earlier are added with Register promoted agents (platform admins).";
  }
  if (status) {
    return `No ${tab === "all" ? "" : `${CATEGORY_LABELS[tab].toLowerCase()} `}records with status ${status}.`;
  }
  if (tab === "all") {
    return "Records are created in the Agent Registry; POST /catalogue/{id}/status approves, rejects or deprecates them.";
  }
  return `No ${CATEGORY_LABELS[tab].toLowerCase()} records: the platform classifies MCP records as tools and CUSTOM records by the kind in their descriptor.`;
}
