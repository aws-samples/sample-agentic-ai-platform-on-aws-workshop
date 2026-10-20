import { describe, expect, it, vi } from "vitest";

import { ApiError, PlatformClient, asList, buildUrl, describeError, errorFromResponse } from "./client";

function jsonResponse(status: number, body: unknown, contentType = "application/json"): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "Content-Type": contentType },
  });
}

function clientWith(fetchImpl: typeof fetch, extra: Partial<ConstructorParameters<typeof PlatformClient>[0]> = {}) {
  return new PlatformClient({ baseUrl: "https://api.example.com/", getToken: () => "id-token", fetchImpl, ...extra });
}

describe("errorFromResponse", () => {
  it("maps the contract error shape to code, message, status and details", () => {
    const error = errorFromResponse(409, {
      error: { code: "REGISTRY_NOT_CONFIGURED", message: "no Agent Registry is configured", details: { module: "4" } },
    });
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("REGISTRY_NOT_CONFIGURED");
    expect(error.message).toBe("no Agent Registry is configured");
    expect(error.status).toBe(409);
    expect(error.details).toEqual({ module: "4" });
    expect(error.toString()).toBe("REGISTRY_NOT_CONFIGURED (409): no Agent Registry is configured");
  });

  it("maps API Gateway's own {message} bodies and plain text bodies to HTTP_<status>", () => {
    const gateway = errorFromResponse(401, { message: "Unauthorized" });
    expect(gateway.code).toBe("HTTP_401");
    expect(gateway.message).toBe("Unauthorized");
    const text = errorFromResponse(502, "<html>Bad gateway</html>");
    expect(text.code).toBe("HTTP_502");
    expect(text.message).toBe("<html>Bad gateway</html>");
    const empty = errorFromResponse(500, undefined, "Internal Server Error");
    expect(empty.message).toBe("Internal Server Error");
  });

  it("falls back to HTTP_<status> when the error object has no code", () => {
    const error = errorFromResponse(400, { error: { message: "bad" } });
    expect(error.code).toBe("HTTP_400");
    expect(error.message).toBe("bad");
  });
});

describe("PlatformClient.request", () => {
  it("sends the bearer token, JSON body and query parameters without empty values", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe("https://api.example.com/metrics?projectId=hr-assistant&env=dev&hours=24");
      expect(init?.method).toBe("GET");
      expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer id-token");
      return jsonResponse(200, { series: {} });
    }) as unknown as typeof fetch;
    const client = clientWith(fetchImpl);
    await expect(client.metrics("hr-assistant", "dev", 24)).resolves.toEqual({ series: {} });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("drops undefined query parameters and posts JSON", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/projects")) {
        expect(init?.method).toBe("POST");
        expect((init?.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
        expect(JSON.parse(String(init?.body))).toEqual({ projectId: "p1", domainId: "d1", name: "P1", models: ["m"] });
        return jsonResponse(201, { projectId: "p1" });
      }
      expect(String(input)).toBe("https://api.example.com/spend?projectId=p1");
      return jsonResponse(200, { keys: [] });
    }) as unknown as typeof fetch;
    const client = clientWith(fetchImpl);
    await client.createProject({ projectId: "p1", domainId: "d1", name: "P1", models: ["m"] });
    await client.spend("p1", undefined);
  });

  it("throws ApiError with the contract code for 4xx answers and notifies on 401", async () => {
    const onUnauthorized = vi.fn();
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/me")) {
        return jsonResponse(401, { message: "Unauthorized" });
      }
      return jsonResponse(403, { error: { code: "FORBIDDEN", message: "this route requires the role approver" } });
    }) as unknown as typeof fetch;
    const client = clientWith(fetchImpl, { onUnauthorized });
    await expect(client.approveRelease("r1")).rejects.toMatchObject({ code: "FORBIDDEN", status: 403 });
    await expect(client.me()).rejects.toMatchObject({ code: "HTTP_401", status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("returns undefined for 204 / empty bodies and rejects non-JSON 2xx bodies", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/domains/")) {
        return new Response(null, { status: 204 });
      }
      return jsonResponse(200, "<!doctype html>", "text/html");
    }) as unknown as typeof fetch;
    const client = clientWith(fetchImpl);
    await expect(client.deleteDomain("d1")).resolves.toBeUndefined();
    await expect(client.health()).rejects.toMatchObject({ code: "BAD_RESPONSE" });
  });

  it("maps network failures and timeouts to NETWORK_ERROR / TIMEOUT", async () => {
    const failing = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch;
    await expect(clientWith(failing).health()).rejects.toMatchObject({ code: "NETWORK_ERROR", status: null });

    const hanging = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    ) as unknown as typeof fetch;
    await expect(clientWith(hanging, { timeoutMs: 20 }).health()).rejects.toMatchObject({ code: "TIMEOUT" });
  });

  it("omits the Authorization header when signed out", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect((init?.headers as Record<string, string>).Authorization).toBeUndefined();
      return jsonResponse(200, { status: "ok", version: "0.4.0" });
    }) as unknown as typeof fetch;
    const client = new PlatformClient({ baseUrl: "https://api.example.com", getToken: () => undefined, fetchImpl });
    await expect(client.health()).resolves.toMatchObject({ version: "0.4.0" });
  });
});

describe("waitForJob", () => {
  it("polls until a terminal state and reports the cap as JOB_TIMEOUT", async () => {
    let calls = 0;
    const fetchImpl = vi.fn(async () => {
      calls += 1;
      return jsonResponse(200, { jobId: "j1", status: calls < 3 ? "RUNNING" : "SUCCEEDED", output: { text: "hi" } });
    }) as unknown as typeof fetch;
    const client = clientWith(fetchImpl);
    const seen: string[] = [];
    const job = await client.waitForJob("j1", { pollMs: 1, onUpdate: (j) => seen.push(j.status) });
    expect(job.status).toBe("SUCCEEDED");
    expect(seen).toEqual(["RUNNING", "RUNNING", "SUCCEEDED"]);

    const stuck = vi.fn(async () => jsonResponse(200, { jobId: "j2", status: "QUEUED" })) as unknown as typeof fetch;
    await expect(clientWith(stuck).waitForJob("j2", { pollMs: 1, timeoutMs: 5 })).rejects.toMatchObject({
      code: "JOB_TIMEOUT",
    });
  });
});

describe("helpers", () => {
  it("asList accepts bare lists and envelopes", () => {
    expect(asList([1, 2])).toEqual([1, 2]);
    expect(asList({ domains: [{ a: 1 }] })).toEqual([{ a: 1 }]);
    expect(asList(null)).toEqual([]);
    expect(() => asList({ nope: 1 })).toThrowError(ApiError);
  });

  it("buildUrl keeps a base path and encodes query values", () => {
    expect(buildUrl("https://api.example.com/stage/", "/catalogue/search", { q: "hr tools", skip: undefined })).toBe(
      "https://api.example.com/stage/catalogue/search?q=hr+tools",
    );
    expect(buildUrl("https://api.example.com", "projects")).toBe("https://api.example.com/projects");
  });

  it("describeError handles ApiError, Error and strings", () => {
    expect(describeError(new ApiError("X", "boom", 500))).toEqual({ code: "X", message: "boom", status: 500 });
    expect(describeError(new Error("plain"))).toEqual({ code: "Error", message: "plain", status: null });
    expect(describeError("text")).toEqual({ code: "ERROR", message: "text", status: null });
  });
});
