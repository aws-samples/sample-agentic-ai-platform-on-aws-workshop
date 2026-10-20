import "@cloudscape-design/global-styles/index.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { ConfigErrorPage } from "./ConfigErrorPage";
import { loadRuntimeConfig } from "./config/runtimeConfig";
import { applyAppTheme } from "./theme";

applyAppTheme();

const root = createRoot(document.getElementById("root")!);

loadRuntimeConfig()
  .then((config) => {
    root.render(
      <StrictMode>
        <App config={config} />
      </StrictMode>,
    );
  })
  .catch((error: unknown) => {
    root.render(
      <StrictMode>
        <ConfigErrorPage error={error} />
      </StrictMode>,
    );
  });
