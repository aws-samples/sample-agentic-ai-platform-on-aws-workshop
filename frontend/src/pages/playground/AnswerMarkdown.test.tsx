import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AnswerMarkdown } from "./AnswerMarkdown";

describe("AnswerMarkdown", () => {
  it("renders markdown without images, raw HTML or same-tab links", () => {
    const { container } = render(
      <AnswerMarkdown>{"**Annual leave**: 12 days ![x](https://example.com/p.png?leak=1) <img src=\"https://example.com/raw.png\"> [policy](https://example.com/policy) [bad](javascript:alert(1))"}</AnswerMarkdown>,
    );
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(container.innerHTML).not.toContain("example.com/p.png");
    expect(container.querySelector("strong")?.textContent).toBe("Annual leave");
    const links = Array.from(container.querySelectorAll("a"));
    const policy = links.find((a) => a.textContent === "policy");
    expect(policy?.getAttribute("href")).toBe("https://example.com/policy");
    expect(policy?.getAttribute("target")).toBe("_blank");
    expect(policy?.getAttribute("rel")).toBe("noopener noreferrer");
    const bad = links.find((a) => a.textContent === "bad");
    expect(bad?.getAttribute("href") ?? "").not.toContain("javascript:");
    expect(container.textContent).toContain("12 days");
  });
});
