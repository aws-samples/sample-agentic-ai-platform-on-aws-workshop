import { describe, expect, it } from "vitest";

import { expandDescriptors } from "./CataloguePage";

describe("expandDescriptors", () => {
  it("parses JSON strings nested in the Agent Registry descriptors", () => {
    const input = {
      mcpServer: {
        data: '{"name": "io.anycompany/hr", "remotes": [{"type": "streamable-http"}]}',
        additionalData: { tools: { data: '{"tools": [{"name": "hrkb___Retrieve"}]}' } },
      },
      plain: "text",
    };
    expect(expandDescriptors(input)).toEqual({
      mcpServer: {
        data: { name: "io.anycompany/hr", remotes: [{ type: "streamable-http" }] },
        additionalData: { tools: { data: { tools: [{ name: "hrkb___Retrieve" }] }} },
      },
      plain: "text",
    });
  });

  it("leaves broken JSON alone", () => {
    expect(expandDescriptors({ data: "{not json" })).toEqual({ data: "{not json" });
  });
});
