import { useEffect, useRef, type ReactNode } from "react";
import { useAuth } from "react-oidc-context";
import { useLocation, useNavigate } from "react-router-dom";

import { safeReturnTo } from "../auth/AppAuthProvider";
import { SignInPage } from "./SignInPage";

/**
 * Renders the application only for a signed-in user. After the Hosted UI callback the router still holds the
 * `/?code=&state=` location, so the gate navigates once to the page the user came from.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const restored = useRef(false);

  useEffect(() => {
    if (!auth.isAuthenticated || restored.current) {
      return;
    }
    const params = new URLSearchParams(location.search);
    if (params.has("code") || params.has("state")) {
      restored.current = true;
      navigate(safeReturnTo(auth.user?.state), { replace: true });
    }
  }, [auth.isAuthenticated, auth.user, location.search, navigate]);

  if (!auth.isAuthenticated) {
    return <SignInPage />;
  }
  return <>{children}</>;
}
