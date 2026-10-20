import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

// https://vite.dev/config/ - the dev server proxies /config.json to the deployed CloudFront URL (UI_URL), so the SPA
// reads the same runtime configuration locally as in production and never ships a config.json of its own.
export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), "UI_"), ...process.env };
  const uiUrl = (env.UI_URL ?? "").replace(/\/+$/, "");
  return {
    plugins: [react()],
    server: {
      port: 5173, // the only localhost callback URL registered on the Cognito app client
      strictPort: true,
      proxy: uiUrl
        ? {
            "/config.json": { target: uiUrl, changeOrigin: true, secure: true },
          }
        : undefined,
    },
    preview: { port: 5173, strictPort: true },
    build: {
      sourcemap: false,
      chunkSizeWarningLimit: 1800,
    },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      css: false,
    },
  };
});
