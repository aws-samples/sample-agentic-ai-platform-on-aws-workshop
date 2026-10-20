import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";

import { ConfigError } from "./config/runtimeConfig";

/** Shown when /config.json is missing, not JSON yet (SPA fallback) or unreachable. */
export function ConfigErrorPage({ error }: { error: unknown }) {
  const kind = error instanceof ConfigError ? error.kind : "invalid";
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "#232F3E" }}>
      <div style={{ width: "min(640px, 92vw)" }}>
        <Container header={<Header variant="h1">AnyCompany Agent Platform</Header>}>
          <Alert type={kind === "not-configured" ? "info" : "error"} header={kind === "not-configured" ? "Not configured yet" : "Configuration error"}>
            <Box>{message}</Box>
            <Box padding={{ top: "s" }}>
              The UI reads <code>/config.json</code> (API URL and Cognito ids) from its own origin. The Platform stack writes it into
              the web bucket; for local development start Vite with <code>UI_URL=https://&lt;cloudfront&gt; npm run dev</code> so the
              dev server proxies it.
            </Box>
          </Alert>
        </Container>
      </div>
    </div>
  );
}
