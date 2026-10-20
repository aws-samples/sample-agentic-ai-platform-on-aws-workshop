/**
 * Runtime configuration of the UI: `/config.json` written into the web bucket by the Platform stack
 * (build contract section 10). Before the Platform stack has deployed, the SPA fallback answers that path with
 * index.html (HTTP 200, text/html), so a non-JSON answer means "not configured" rather than "broken".
 */
export interface RuntimeConfig {
  apiUrl: string;
  region: string;
  userPoolId: string;
  clientId: string;
  authDomain: string;
  modelGatewayUrl: string;
  uiUrl: string;
}

export type ConfigErrorKind = "not-configured" | "invalid" | "unreachable";

export class ConfigError extends Error {
  readonly kind: ConfigErrorKind;

  constructor(kind: ConfigErrorKind, message: string) {
    super(message);
    this.name = "ConfigError";
    this.kind = kind;
  }
}

export const REQUIRED_CONFIG_KEYS: ReadonlyArray<keyof RuntimeConfig> = [
  "apiUrl",
  "region",
  "userPoolId",
  "clientId",
  "authDomain",
];

const OPTIONAL_CONFIG_KEYS: ReadonlyArray<keyof RuntimeConfig> = ["modelGatewayUrl", "uiUrl"];

/** Parse the body of `/config.json`; throws ConfigError("not-configured") for the SPA fallback HTML. */
export function parseRuntimeConfig(text: string): RuntimeConfig {
  const trimmed = text.trim();
  if (!trimmed.startsWith("{")) {
    throw new ConfigError(
      "not-configured",
      "config.json is not JSON yet: the Platform stack writes it after the Web stack (deploy AgentPlatform-Platform).",
    );
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (error) {
    throw new ConfigError("invalid", `config.json is not valid JSON: ${(error as Error).message}`);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new ConfigError("invalid", "config.json must be a JSON object");
  }
  const record = parsed as Record<string, unknown>;
  const missing = REQUIRED_CONFIG_KEYS.filter((key) => typeof record[key] !== "string" || !record[key]);
  if (missing.length) {
    throw new ConfigError("invalid", `config.json is missing ${missing.join(", ")}`);
  }
  const config: Record<string, string> = {};
  for (const key of [...REQUIRED_CONFIG_KEYS, ...OPTIONAL_CONFIG_KEYS]) {
    const value = record[key];
    config[key] = typeof value === "string" ? value.replace(/\/+$/, "") : "";
  }
  return config as unknown as RuntimeConfig;
}

/** Fetch and parse `/config.json` from the origin that served the application. */
export async function loadRuntimeConfig(fetchImpl: typeof fetch = fetch): Promise<RuntimeConfig> {
  let response: Response;
  try {
    response = await fetchImpl("/config.json", { cache: "no-cache", headers: { Accept: "application/json" } });
  } catch (error) {
    throw new ConfigError("unreachable", `config.json could not be fetched: ${(error as Error).message}`);
  }
  if (!response.ok) {
    throw new ConfigError("unreachable", `config.json answered HTTP ${response.status}`);
  }
  return parseRuntimeConfig(await response.text());
}

/** OIDC issuer of the Cognito user pool (the `authority` of oidc-client-ts). */
export function cognitoAuthority(config: Pick<RuntimeConfig, "region" | "userPoolId">): string {
  return `https://cognito-idp.${config.region}.amazonaws.com/${config.userPoolId}`;
}

/** Cognito Hosted UI logout URL (Cognito publishes no end_session_endpoint, so the UI builds it). */
export function cognitoLogoutUrl(config: Pick<RuntimeConfig, "authDomain" | "clientId">, origin: string): string {
  const params = new URLSearchParams({ client_id: config.clientId, logout_uri: `${origin}/` });
  return `${config.authDomain}/logout?${params.toString()}`;
}
