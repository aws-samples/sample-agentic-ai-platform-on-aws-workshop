import type { MultiselectProps } from "@cloudscape-design/components/multiselect";

import type { KnowledgeBaseOption, ToolOption } from "../../api/types";

/** `arn:aws:lambda:<region>:<account>:function:<name>[:<qualifier>]` -> `<name>` (mirror of the backend's lambda_function_name). */
export function lambdaFunctionName(arn: string): string {
  const tail = arn.includes(":function:") ? arn.split(":function:")[1] : (arn.split(":").pop() ?? arn);
  return tail.split(":")[0] || arn;
}

/** A knowledge base id from an id or a `...:knowledge-base/<id>` ARN. */
export function knowledgeBaseId(value: string): string {
  return value.startsWith("arn:") ? (value.split("/").pop() ?? value) : value;
}

const TAG_MAX = 72;

function tag(text: string | null | undefined): string[] | undefined {
  if (!text) {
    return undefined;
  }
  return [text.length > TAG_MAX ? `${text.slice(0, TAG_MAX - 1)}…` : text];
}

/** The candidates the form may offer: a row that exists only in some project's allow-list (`inventoried: false`, a deleted
 * Lambda or a mistyped id) is not one of them; the owning project's own value still appears through the fallback below. */
function inventoried<T extends { inventoried?: boolean }>(candidates: T[] | undefined): T[] {
  return (candidates ?? []).filter((candidate) => candidate.inventoried !== false);
}

/**
 * Options of the tool pick list: the name on the first line, the ARN as the muted second line. The project's current
 * values that are not among the candidates stay selectable (labelled from the ARN) so an edit never drops them silently.
 */
export function toolPickerOptions(candidates: ToolOption[] | undefined, current: ReadonlyArray<string>): MultiselectProps.Option[] {
  // while the candidates are still loading (or failed to load) a current value is simply unknown, not "not in the list"
  const fallbackTags = candidates ? ["not in the pick list"] : undefined;
  const options: MultiselectProps.Option[] = inventoried(candidates).map((tool) => ({
    value: tool.arn,
    label: tool.name,
    description: tool.arn,
    tags: tag(tool.description),
  }));
  for (const arn of current) {
    if (!options.some((option) => option.value === arn)) {
      options.push({ value: arn, label: lambdaFunctionName(arn), description: arn, tags: fallbackTags });
    }
  }
  return options;
}

/** Options of the knowledge base pick list: the name first, the id as the muted second line. */
export function knowledgeBasePickerOptions(
  candidates: KnowledgeBaseOption[] | undefined,
  current: ReadonlyArray<string>,
): MultiselectProps.Option[] {
  const fallbackTags = candidates ? ["not in the pick list"] : undefined;
  const options: MultiselectProps.Option[] = inventoried(candidates).map((kb) => ({
    value: kb.id,
    label: kb.name,
    description: kb.id,
    tags: tag(kb.description ?? (kb.status && kb.status !== "ACTIVE" ? `status ${kb.status}` : undefined)),
  }));
  for (const value of current) {
    const id = knowledgeBaseId(value);
    if (!options.some((option) => option.value === id || option.value === value)) {
      options.push({ value, label: id, description: value, tags: fallbackTags });
    }
  }
  return options;
}

/** The option objects (from `options`) for `values`, in the order of `values`; unknown values are skipped. */
export function selectedOptions(options: ReadonlyArray<MultiselectProps.Option>, values: ReadonlyArray<string>): MultiselectProps.Option[] {
  return values
    .map((value) => options.find((option) => option.value === value) ?? options.find((option) => knowledgeBaseId(option.value ?? "") === knowledgeBaseId(value)))
    .filter((option): option is MultiselectProps.Option => option !== undefined);
}

/** The values of the selected options (what the API receives), de-duplicated. */
export function valuesOf(selected: ReadonlyArray<MultiselectProps.Option>): string[] {
  return Array.from(new Set(selected.map((option) => option.value).filter((value): value is string => typeof value === "string" && value.length > 0)));
}

/** True when both lists hold the same set of values (order and duplicates ignored): a re-ticked option is not a change. */
export function sameValues(a: ReadonlyArray<string>, b: ReadonlyArray<string>): boolean {
  const left = Array.from(new Set(a)).sort();
  const right = Array.from(new Set(b)).sort();
  return left.length === right.length && left.every((value, index) => value === right[index]);
}
