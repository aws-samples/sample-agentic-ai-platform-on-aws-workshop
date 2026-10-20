import { createContext, useContext } from "react";

import type { RuntimeConfig } from "./runtimeConfig";

export const ConfigContext = createContext<RuntimeConfig | null>(null);

export function useConfig(): RuntimeConfig {
  const config = useContext(ConfigContext);
  if (!config) {
    throw new Error("useConfig must be used inside ConfigContext.Provider");
  }
  return config;
}
