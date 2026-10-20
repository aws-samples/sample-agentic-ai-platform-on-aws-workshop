import { createContext, useContext, useMemo, type ReactNode } from "react";
import { useAuth } from "react-oidc-context";

import { useMeQuery } from "../api/hooks";
import type { Me } from "../api/types";
import { can as canDo, canAccessScreen as canSee, displayRole, rolesOf, type Action, type Role, type Screen } from "./roles";

export interface MeContextValue {
  me: Me | undefined;
  roles: Role[];
  role: Role | null;
  isLoading: boolean;
  error: Error | null;
  can: (action: Action) => boolean;
  canAccessScreen: (screen: Screen) => boolean;
  refetch: () => void;
}

const MeContext = createContext<MeContextValue | null>(null);

/** GET /me after sign-in; `roles[]` drives every navigation entry and action button. */
export function MeProvider({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const query = useMeQuery(auth.isAuthenticated);
  const value = useMemo<MeContextValue>(() => {
    const roles = rolesOf(query.data);
    return {
      me: query.data,
      roles,
      role: displayRole(query.data),
      isLoading: auth.isAuthenticated && query.isLoading,
      error: query.error ?? null,
      can: (action) => canDo(roles, action),
      canAccessScreen: (screen) => canSee(roles, screen),
      refetch: () => void query.refetch(),
    };
  }, [auth.isAuthenticated, query]);
  return <MeContext.Provider value={value}>{children}</MeContext.Provider>;
}

export function useMe(): MeContextValue {
  const value = useContext(MeContext);
  if (!value) {
    throw new Error("useMe must be used inside MeProvider");
  }
  return value;
}
