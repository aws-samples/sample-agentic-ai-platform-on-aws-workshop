import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { INFO_SPACER, InfoFormField, InfoHeader, InfoPopover } from "./InfoPopover";

afterEach(cleanup);

const HELP = "One LiteLLM virtual key per agent, environment and model.";
const visibleCopies = () => screen.queryAllByText(HELP).filter((el) => !isHiddenForScreenReaders(el)).length;
const isHiddenForScreenReaders = (el: HTMLElement) => el.closest("span[style*='clip']") !== null;

describe("InfoPopover", () => {
  it("renders a round info button named after its heading, shows the text on hover and pins it on click", () => {
    render(<InfoPopover label="Keys" content={HELP} />);
    const button = screen.getByRole("button", { name: "Information about Keys" });
    expect(button).toBeInTheDocument();
    expect(button).not.toHaveAttribute("title"); // no second, native tooltip on top of ours
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(visibleCopies()).toBe(0);
    fireEvent.mouseEnter(screen.getByTestId("info-popover"));
    expect(screen.getByRole("tooltip")).toHaveTextContent(HELP);
    fireEvent.mouseLeave(screen.getByTestId("info-popover"));
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.click(button);
    expect(visibleCopies()).toBe(1); // the popover
    expect(button).toHaveAttribute("aria-expanded", "true");
  });

  it("never shows the tooltip beside the pinned popover, and hovers again once the popover has closed", () => {
    render(<InfoPopover label="Keys" content={HELP} />);
    const wrapper = screen.getByTestId("info-popover");
    fireEvent.mouseEnter(wrapper);
    fireEvent.click(screen.getByRole("button", { name: "Information about Keys" }));
    fireEvent.mouseLeave(wrapper);
    fireEvent.mouseEnter(wrapper); // back onto the icon while the popover is still open
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(visibleCopies()).toBe(1);
    fireEvent.mouseLeave(wrapper);
    fireEvent.mouseDown(document.body); // Cloudscape closes the popover on an outside mousedown
    expect(visibleCopies()).toBe(0);
    expect(screen.getByRole("button", { name: "Information about Keys" })).toHaveAttribute("aria-expanded", "false");
    fireEvent.mouseEnter(wrapper);
    expect(screen.getByRole("tooltip")).toHaveTextContent(HELP);
    fireEvent.keyDown(document.body, { key: "Escape" }); // Escape dismisses the tooltip (so a second Escape can reach a modal)
  });
});

describe("InfoHeader", () => {
  it("puts the help behind the icon, names the icon after the heading and keeps the description slot with the text hidden", () => {
    const { container } = render(
      <InfoHeader help="Evaluation verdicts, approvals and promotions happen on the Releases screen." counter="(3)">
        Releases
      </InfoHeader>,
    );
    expect(screen.getByRole("heading", { level: 2, name: /Releases/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Information about Releases" })).toBeInTheDocument();
    const description = container.querySelector("p[class*='description']");
    expect(description).not.toBeNull();
    expect(description?.textContent?.endsWith(INFO_SPACER)).toBe(true); // the slot keeps one line of height
    const hidden = screen.getByText("Evaluation verdicts, approvals and promotions happen on the Releases screen.");
    expect(isHiddenForScreenReaders(hidden)).toBe(true); // present for assistive technology, not drawn
  });

  it("keeps a visible description when one is given (values that must stay on the page) and takes an explicit label", () => {
    render(
      <InfoHeader help="Defaults applied when a project or key is created without explicit values." description="Last change 2026-10-19" label="Default limits">
        <span>Default limits and caps</span>
      </InfoHeader>,
    );
    expect(screen.getByText("Last change 2026-10-19")).toBeInTheDocument();
    expect(screen.queryByText("Defaults applied when a project or key is created without explicit values.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Information about Default limits" })).toBeInTheDocument();
  });
});

describe("InfoFormField", () => {
  it("moves the field description behind the icon next to the label and keeps the control's accessible description", () => {
    const { container } = render(
      <InfoFormField label="Key cap (USD)" help="LiteLLM max_budget; default from the platform settings" constraintText="USD">
        <input aria-label="cap" />
      </InfoFormField>,
    );
    expect(screen.getByText("Key cap (USD)")).toBeInTheDocument();
    expect(screen.getByText("USD")).toBeInTheDocument(); // constraintText still passes through
    expect(screen.getByRole("button", { name: "Information about Key cap (USD)" })).toBeInTheDocument();
    const description = container.querySelector("div[id$='-description']");
    expect(description?.textContent?.endsWith(INFO_SPACER)).toBe(true);
    expect(isHiddenForScreenReaders(screen.getByText("LiteLLM max_budget; default from the platform settings"))).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Information about Key cap (USD)" }));
    expect(screen.getAllByText("LiteLLM max_budget; default from the platform settings").filter((el) => !isHiddenForScreenReaders(el)).length).toBe(1);
  });
});
