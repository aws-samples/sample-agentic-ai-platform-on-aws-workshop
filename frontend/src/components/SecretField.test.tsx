import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SecretField } from "./SecretField";

// vitest runs without `globals`, so testing-library's automatic unmount between tests is not registered
afterEach(cleanup);

describe("SecretField", () => {
  it("masks the value until the eye button is clicked, then hides it again", () => {
    render(<SecretField value="sk-secret-value" label="LiteLLM admin password" />);
    const input = screen.getByLabelText("LiteLLM admin password") as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(input.value).toBe("sk-secret-value");
    expect(input.readOnly).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Show LiteLLM admin password" }));
    expect((screen.getByLabelText("LiteLLM admin password") as HTMLInputElement).type).toBe("text");
    fireEvent.click(screen.getByRole("button", { name: "Hide LiteLLM admin password" }));
    expect((screen.getByLabelText("LiteLLM admin password") as HTMLInputElement).type).toBe("password");
  });

  it("offers a copy button that never reveals the value", () => {
    render(<SecretField value="sk-secret-value" label="LiteLLM admin password" />);
    // Cloudscape's CopyToClipboard renders the button inside a popover trigger, so the name can resolve to two elements
    expect(screen.getAllByRole("button", { name: "Copy LiteLLM admin password" }).length).toBeGreaterThan(0);
    expect((screen.getByLabelText("LiteLLM admin password") as HTMLInputElement).type).toBe("password");
  });

  it("shows loading, error and empty states", () => {
    const { rerender } = render(<SecretField value={undefined} label="x" loading />);
    expect(screen.getByText(/Loading/)).toBeInTheDocument();
    rerender(<SecretField value={undefined} label="x" error="FORBIDDEN (403): admins only" />);
    expect(screen.getByText(/admins only/)).toBeInTheDocument();
    rerender(<SecretField value={null} label="x" />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
