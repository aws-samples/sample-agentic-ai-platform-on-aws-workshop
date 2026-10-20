import { describe, expect, it } from "vitest";

import { chartPeriod, periodLabel, rebucket } from "./charts";

describe("chart bucketing", () => {
  it("picks at most 48 bars with a multiple of the API period", () => {
    expect(chartPeriod(1, 60)).toBe(120);
    expect(chartPeriod(24, 300)).toBe(1800);
    expect(chartPeriod(168, 3600)).toBe(14_400);
    expect(periodLabel(1800)).toBe("30 min");
    expect(periodLabel(14_400)).toBe("4 h");
    expect(periodLabel(86_400)).toBe("1 d");
  });

  it("fills empty buckets with zero and sums or maxes the points inside a bucket", () => {
    const domain: [Date, Date] = [new Date("2026-10-09T00:00:00Z"), new Date("2026-10-09T02:00:00Z")];
    const series: Array<[string, number]> = [
      ["2026-10-09T00:05:00Z", 2],
      ["2026-10-09T00:10:00Z", 3],
      ["2026-10-09T01:40:00Z", 1],
      ["2026-10-08T23:00:00Z", 99], // outside the window: ignored
    ];
    const sums = rebucket(series, domain, 1800, "sum");
    expect(sums).toHaveLength(4);
    expect(sums.map((p) => p.y)).toEqual([5, 0, 0, 1]);
    expect(sums[0].x.toISOString()).toBe("2026-10-09T00:00:00.000Z");
    const maxes = rebucket(series, domain, 1800, "max");
    expect(maxes.map((p) => p.y)).toEqual([3, 0, 0, 1]);
    expect(rebucket(undefined, domain, 3600)).toHaveLength(2);
  });
});
