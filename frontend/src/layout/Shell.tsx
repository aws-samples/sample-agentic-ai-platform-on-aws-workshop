import AppLayout, { type AppLayoutProps } from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Flashbar from "@cloudscape-design/components/flashbar";
import SideNavigation, { type SideNavigationProps } from "@cloudscape-design/components/side-navigation";
import TopNavigation, { type TopNavigationProps } from "@cloudscape-design/components/top-navigation";
import { useEffect, useRef, useState } from "react";
import { useAuth } from "react-oidc-context";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

import { useApi } from "../api/ApiProvider";
import { useMe } from "../auth/MeContext";
import { roleLabel, type Screen } from "../auth/roles";
import { signOut } from "../auth/signOut";
import { personaFromItemId, personaLabel, personaMenuItems, switchPersona, takeSwitchedPersona } from "../auth/switchPersona";
import type { RuntimeConfig } from "../config/runtimeConfig";
import { useBreadcrumbsValue } from "./breadcrumbs";
import { formatErrorContent, useNotifications } from "./notifications";
import { useSplitPanel } from "./splitPanel";

export const APP_TITLE = "AnyCompany Agent Platform";

export const NAV_ITEMS: ReadonlyArray<{ screen: Screen; text: string; href: string }> = [
  { screen: "overview", text: "Overview", href: "/" },
  { screen: "domains", text: "Domains and projects", href: "/domains" },
  { screen: "catalogue", text: "Catalogue", href: "/catalogue" },
  { screen: "governance", text: "Governance", href: "/governance" },
  { screen: "releases", text: "Releases", href: "/releases" },
  { screen: "playground", text: "Playground", href: "/playground" },
];

/** The side navigation entry a path belongs to ("/projects/x" belongs to Domains and projects). */
export function activeNavHref(pathname: string): string {
  if (pathname === "/") {
    return "/";
  }
  if (pathname.startsWith("/projects") || pathname.startsWith("/domains")) {
    return "/domains";
  }
  const match = NAV_ITEMS.find((item) => item.href !== "/" && pathname.startsWith(item.href));
  return match?.href ?? "/";
}

export function Shell({ config }: { config: RuntimeConfig }) {
  const auth = useAuth();
  const api = useApi();
  const me = useMe();
  const navigate = useNavigate();
  const location = useLocation();
  const { crumbs } = useBreadcrumbsValue();
  const { items: notifications, notify, success } = useNotifications();
  const splitPanel = useSplitPanel();
  const [navigationOpen, setNavigationOpen] = useState(true);
  // Cloudscape's toolbar AppLayout closes the navigation whenever its measured width is momentarily smaller than the
  // navigation + padding (seen intermittently after full page loads with charts). Only a user gesture may close it on a
  // desktop viewport; on narrow viewports the automatic close is kept so the content stays visible.
  const lastInteraction = useRef(0);
  useEffect(() => {
    const mark = () => {
      lastInteraction.current = Date.now();
    };
    window.addEventListener("pointerdown", mark, true);
    window.addEventListener("keydown", mark, true);
    return () => {
      window.removeEventListener("pointerdown", mark, true);
      window.removeEventListener("keydown", mark, true);
    };
  }, []);
  const onNavigationChange = (open: boolean) => {
    const userDriven = Date.now() - lastInteraction.current < 1500;
    const narrowViewport = window.innerWidth < 1024;
    if (!open && !userDriven && !narrowViewport) {
      return;
    }
    setNavigationOpen(open);
  };
  const [splitPanelPreferences, setSplitPanelPreferences] = useState<AppLayoutProps.SplitPanelPreferences>({ position: "side" });
  // One confirmation after a persona switch: switchPersona stores the persona right before it loads the page again, so only the
  // first run after a mount may read the marker. A re-run in the outgoing document (the dependencies change while the browser is
  // still starting the reload) would otherwise consume it before the new document can show the confirmation. The signed-in user
  // is checked because a token renewal of the outgoing persona that was already in flight could have been stored last.
  const confirmedSwitch = useRef(false);
  useEffect(() => {
    if (confirmedSwitch.current) {
      return;
    }
    confirmedSwitch.current = true;
    const persona = takeSwitchedPersona();
    if (!persona) {
      return;
    }
    if (auth.user?.profile?.["cognito:username"] === persona) {
      success(`You are now ${personaLabel(persona)}`);
    } else {
      notify({
        type: "warning",
        header: `The switch to ${personaLabel(persona)} did not take effect`,
        content: "Pick the persona again from the user menu.",
      });
    }
  }, [auth.user, notify, success]);
  const switching = useRef(false);

  const navItems: SideNavigationProps.Item[] = NAV_ITEMS.filter((item) => me.canAccessScreen(item.screen)).map((item) => ({
    type: "link",
    text: item.text,
    href: item.href,
  }));

  // the persona usernames are the role names (admin, developer, approver); the ID-token claim covers the moment before GET /me answers
  const currentPersona = me.me?.username ?? (auth.user?.profile?.["cognito:username"] as string | undefined);
  const username = currentPersona ?? auth.user?.profile?.email ?? "Signed in";
  const email = me.me?.email ?? auth.user?.profile?.email ?? undefined;
  const utilities: TopNavigationProps.Utility[] = [
    {
      type: "menu-dropdown",
      text: `${username} (${roleLabel(me.role)})`,
      description: email ? String(email) : undefined,
      iconName: "user-profile",
      items: [
        {
          id: "roles",
          text: me.roles.length ? `Roles: ${me.roles.join(", ")}` : "Roles: none (no platform group)",
          disabled: true,
        },
        {
          id: "groups",
          text: me.me?.groups?.length ? `Groups: ${me.me.groups.join(", ")}` : "Groups: none",
          disabled: true,
        },
        // Workshop shortcut: become another persona without its password (POST /me/switch mints the tokens, see src/auth/switchPersona.ts)
        { id: "switch", text: "Switch persona", items: personaMenuItems(currentPersona) },
        { id: "signout", text: "Sign out" },
      ],
      onItemClick: ({ detail }) => {
        if (switching.current) {
          return; // a switch is in flight and the page is about to load again: no sign-out and no second switch meanwhile
        }
        if (detail.id === "signout") {
          void signOut(auth, config);
          return;
        }
        const persona = personaFromItemId(detail.id);
        // the collapsed (narrow viewport) top navigation ignores `disabled`, so the current persona is guarded here too
        if (!persona || persona === currentPersona) {
          return;
        }
        switching.current = true;
        notify({ id: "switch-persona", type: "info", loading: true, dismissible: false, header: `Switching to ${personaLabel(persona)}...` });
        switchPersona({ api, auth, config, persona }).catch((error: unknown) => {
          switching.current = false;
          notify({ id: "switch-persona", type: "error", header: "Could not switch persona", content: formatErrorContent(error) });
        });
      },
    },
  ];

  return (
    <>
      <div id="app-top-navigation" style={{ position: "sticky", top: 0, zIndex: 1002 }}>
        <TopNavigation
          identity={{
            href: "/",
            title: APP_TITLE,
            onFollow: (event) => {
              event.preventDefault();
              navigate("/");
            },
          }}
          utilities={utilities}
        />
      </div>
      <AppLayout
        headerSelector="#app-top-navigation"
        toolsHide
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => onNavigationChange(detail.open)}
        navigation={
          <SideNavigation
            header={{ href: "/", text: "Control plane" }}
            activeHref={activeNavHref(location.pathname)}
            items={navItems}
            onFollow={(event) => {
              if (!event.detail.external) {
                event.preventDefault();
                navigate(event.detail.href);
              }
            }}
          />
        }
        breadcrumbs={
          <BreadcrumbGroup
            items={crumbs}
            onFollow={(event) => {
              event.preventDefault();
              navigate(event.detail.href);
            }}
          />
        }
        notifications={<Flashbar items={notifications} stackItems />}
        stickyNotifications
        splitPanel={splitPanel.panel ?? undefined}
        splitPanelOpen={!!splitPanel.panel && splitPanel.open}
        onSplitPanelToggle={({ detail }) => splitPanel.setOpen(detail.open)}
        splitPanelPreferences={splitPanelPreferences}
        onSplitPanelPreferencesChange={({ detail }) => setSplitPanelPreferences(detail)}
        content={<Outlet />}
      />
    </>
  );
}
