import { describe, expect, it } from "vitest";

import {
  formatBudgetDuration,
  formatCompact,
  formatMillis,
  formatPercent,
  formatPricePerMillion,
  formatRelative,
  formatSeconds,
  formatUsd,
  shortId,
  spendPercent,
} from "./format";

describe("money", () => {
  it("formats USD with sub-cent precision for LiteLLM spend", () => {
    expect(formatUsd(0.0009196)).toBe("$0.0009");
    expect(formatUsd(1)).toBe("$1.00");
    expect(formatUsd(2.5)).toBe("$2.50");
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd("0.25")).toBe("$0.25");
    expect(formatUsd(null)).toBe("—");
    expect(formatUsd("abc")).toBe("—");
  });

  it("converts per-token prices to per million tokens", () => {
    expect(formatPricePerMillion(2.2e-6)).toBe("$2.20 / 1M tokens");
    expect(formatPricePerMillion(1.1e-5)).toBe("$11.0 / 1M tokens");
    expect(formatPricePerMillion(undefined)).toBe("—");
  });

  it("computes spend percentages for progress bars", () => {
    expect(spendPercent(0.5, 1)).toBe(50);
    expect(spendPercent(3, 1)).toBe(100);
    expect(spendPercent(1, 0)).toBeNull();
    expect(spendPercent(null, 1)).toBeNull();
  });
});

describe("durations", () => {
  it("formats seconds and milliseconds", () => {
    expect(formatMillis(320)).toBe("320 ms");
    expect(formatMillis(1500)).toBe("1.5 s");
    expect(formatSeconds(31.8)).toBe("31.8 s");
    expect(formatSeconds(125)).toBe("2 min 5 s");
    expect(formatSeconds(3720)).toBe("1 h 2 min");
    expect(formatSeconds(90000)).toBe("1 d 1 h");
    expect(formatSeconds(null)).toBe("—");
  });

  it("spells out LiteLLM budget durations", () => {
    expect(formatBudgetDuration("7d")).toBe("7 days");
    expect(formatBudgetDuration("24h")).toBe("24 hours");
    expect(formatBudgetDuration("1mo")).toBe("1 month");
    expect(formatBudgetDuration("30s")).toBe("30 seconds");
    expect(formatBudgetDuration("weekly")).toBe("weekly");
    expect(formatBudgetDuration(undefined)).toBe("—");
  });

  it("formats relative times", () => {
    const now = new Date("2026-10-09T12:00:00Z");
    expect(formatRelative("2026-10-12T00:00:00Z", now)).toBe("in 3 days");
    expect(formatRelative("2026-10-09T10:00:00Z", now)).toBe("2 h ago");
    expect(formatRelative("2026-10-09T11:58:00Z", now)).toBe("2 min ago");
    expect(formatRelative("not a date", now)).toBe("—");
  });
});

describe("counts and ids", () => {
  it("formats compact numbers and percentages", () => {
    expect(formatCompact(42)).toBe("42");
    expect(formatCompact(1234)).toBe("1.2k");
    expect(formatCompact(25_000)).toBe("25k");
    expect(formatCompact(1_500_000)).toBe("1.5M");
    expect(formatPercent(0.1234)).toBe("12.3%");
    expect(formatPercent(null)).toBe("—");
  });

  it("shortens ARNs to their last segment", () => {
    expect(shortId("arn:aws:bedrock-agentcore:us-east-1:1:gateway/hr-assistant-dev-gw-abc")).toBe("hr-assistant-dev-gw-abc");
    expect(shortId("arn:aws:iam::1:role/agent-platform-proj-hr-assistant-dev-agent")).toBe(
      "agent-platform-proj-hr-assistant-dev-agent",
    );
    expect(shortId(null)).toBe("—");
  });
});

describe("timestamps", () => {
  it("prints the zone name so UTC values from the API and the workshop command can be matched", async () => {
    const { formatDateTime, formatTime } = await import("./format");
    const text = formatDateTime("2026-10-12T00:00:00Z");
    expect(text).toMatch(/^Oct 1[12], 2026, \d{2}:\d{2} [A-Z]{2,5}(\+\d+)?$|^Oct 1[12], 2026, \d{2}:\d{2} GMT[+-]\d+(:\d+)?$/);
    expect(formatTime("2026-10-12T00:00:00Z")).toMatch(/\d{2}:\d{2} \S+/);
    expect(formatDateTime(null)).toBe("—");
  });
});
