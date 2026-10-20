import I18nProvider from "@cloudscape-design/components/i18n";
import enMessages from "@cloudscape-design/components/i18n/messages/all.en";
import { QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { ApiProvider } from "./api/ApiProvider";
import { createQueryClient } from "./api/queryClient";
import { AppAuthProvider } from "./auth/AppAuthProvider";
import { MeProvider } from "./auth/MeContext";
import type { RuntimeConfig } from "./config/runtimeConfig";
import { AuthGate } from "./layout/AuthGate";
import { BreadcrumbsProvider } from "./layout/breadcrumbs";
import { NotFoundPage } from "./layout/NotFoundPage";
import { NotificationsProvider } from "./layout/notifications";
import { RequireRole } from "./layout/RequireRole";
import { Shell } from "./layout/Shell";
import { SplitPanelProvider } from "./layout/splitPanel";
import { CataloguePage } from "./pages/catalogue/CataloguePage";
import { DomainsProjectsPage } from "./pages/domains/DomainsProjectsPage";
import { ProjectDetailPage } from "./pages/domains/ProjectDetailPage";
import { GovernancePage } from "./pages/governance/GovernancePage";
import { OverviewPage } from "./pages/overview/OverviewPage";
import { PlaygroundPage } from "./pages/playground/PlaygroundPage";
import { ReleaseDetailPage } from "./pages/releases/ReleaseDetailPage";
import { ReleasesPage } from "./pages/releases/ReleasesPage";
import { ConfigContext } from "./config/ConfigContext";

export function App({ config }: { config: RuntimeConfig }) {
  const [queryClient] = useState(createQueryClient);
  return (
    <I18nProvider locale="en" messages={[enMessages]}>
      <ConfigContext.Provider value={config}>
        <QueryClientProvider client={queryClient}>
          <AppAuthProvider config={config}>
            <ApiProvider config={config}>
              <MeProvider>
                <BrowserRouter>
                  <NotificationsProvider>
                    <BreadcrumbsProvider>
                      <SplitPanelProvider>
                        <AuthGate>
                          <Routes>
                            <Route element={<Shell config={config} />}>
                              <Route path="/" element={<OverviewPage />} />
                              <Route
                                path="/domains"
                                element={
                                  <RequireRole screen="domains">
                                    <DomainsProjectsPage />
                                  </RequireRole>
                                }
                              />
                              <Route
                                path="/projects/:projectId"
                                element={
                                  <RequireRole screen="domains">
                                    <ProjectDetailPage />
                                  </RequireRole>
                                }
                              />
                              <Route
                                path="/catalogue"
                                element={
                                  <RequireRole screen="catalogue">
                                    <CataloguePage />
                                  </RequireRole>
                                }
                              />
                              <Route
                                path="/governance"
                                element={
                                  <RequireRole screen="governance">
                                    <GovernancePage />
                                  </RequireRole>
                                }
                              />
                              <Route
                                path="/releases"
                                element={
                                  <RequireRole screen="releases">
                                    <ReleasesPage />
                                  </RequireRole>
                                }
                              />
                              <Route
                                path="/releases/:releaseId"
                                element={
                                  <RequireRole screen="releases">
                                    <ReleaseDetailPage />
                                  </RequireRole>
                                }
                              />
                              <Route
                                path="/playground"
                                element={
                                  <RequireRole screen="playground">
                                    <PlaygroundPage />
                                  </RequireRole>
                                }
                              />
                              <Route path="*" element={<NotFoundPage />} />
                            </Route>
                          </Routes>
                        </AuthGate>
                      </SplitPanelProvider>
                    </BreadcrumbsProvider>
                  </NotificationsProvider>
                </BrowserRouter>
              </MeProvider>
            </ApiProvider>
          </AppAuthProvider>
        </QueryClientProvider>
      </ConfigContext.Provider>
    </I18nProvider>
  );
}
