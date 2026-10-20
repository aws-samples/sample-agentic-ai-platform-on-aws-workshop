import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Spinner from "@cloudscape-design/components/spinner";
import type { ReactNode } from "react";

import { useMe } from "../auth/MeContext";
import { roleLabel, SCREEN_ROLES, type Screen } from "../auth/roles";
import { ErrorAlert } from "../components/ErrorAlert";

/** Shows the page only to roles that may use the screen; everyone else sees why not (the API would answer 403 anyway). */
export function RequireRole({ screen, children }: { screen: Screen; children: ReactNode }) {
  const me = useMe();
  if (me.isLoading) {
    return (
      <Box padding="xxl" textAlign="center">
        <Spinner size="large" /> <Box variant="span">Loading your profile…</Box>
      </Box>
    );
  }
  if (me.error) {
    return <ErrorAlert header="Your profile could not be loaded (GET /me)" error={me.error} onRetry={me.refetch} />;
  }
  if (!me.canAccessScreen(screen)) {
    const allowed = SCREEN_ROLES[screen].map(roleLabel).join(", ");
    const personas = SCREEN_ROLES[screen].join(" or "); // the persona usernames are the role names (admin, developer, approver)
    return (
      <Alert type="info" header="This screen is not part of your role">
        You are signed in as {roleLabel(me.role)}. The {screen} screen is available to: {allowed}. Switch persona to {personas} from the
        user menu (top right) to use it; the API enforces the same rule.
      </Alert>
    );
  }
  return <>{children}</>;
}
