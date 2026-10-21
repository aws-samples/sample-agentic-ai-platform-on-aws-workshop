import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import { useAuth } from "react-oidc-context";

import { currentReturnTo } from "../auth/AppAuthProvider";
import { VENDOR_MARKS } from "../components/vendorMarks";
import { APP_TITLE } from "./Shell";

/** Icon size in px. Cloudscape's default button icon is 16px; the logo is wide, so 20px keeps it legible. */
const AWS_LOGO_SIZE = 26;

/**
 * AWS logo (Simple Icons v11, CC0). It is a filled shape, so it is wrapped in a <span>:
 * when an <svg> is the root of iconSvg, Cloudscape forces stroke=currentColor, fill=none
 * and a stroke width, which turns the filled letters into a thick, illegible outline.
 */
const AWS_LOGO = (
  // Height matches the button's 20px line-height, so an icon larger than the text is centred on the text line.
  <span style={{ display: "inline-flex", alignItems: "center", height: 20, verticalAlign: "middle", marginInlineStart: "-30px" }}>
    <svg viewBox="0 0 24 24" width={AWS_LOGO_SIZE} height={AWS_LOGO_SIZE} focusable="false" aria-hidden="true">
      <path
        fill="#000000"
        stroke="none"
        d={VENDOR_MARKS.aws.path}
      />
    </svg>
  </span>
);

/** Button height = 20px line-height + 2 x paddingBlock. Cloudscape default is 4px (28px tall); 12px gives 44px. */
const SIGN_IN_BUTTON_STYLE = { root: { paddingBlock: "10px" } };

/** Landing page when no user is signed in: one button to the Cognito Hosted UI. */
export function SignInPage() {
  const auth = useAuth();
  const busy = auth.isLoading || !!auth.activeNavigator;
  const signIn = () => {
    void auth.signinRedirect({ state: { returnTo: currentReturnTo() } });
  };
  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "#232F3E" }}>
      <div style={{ width: "min(520px, 92vw)" }}>
        <Container
          header={
            <Header variant="h1" description="">
              {APP_TITLE}
            </Header>
          }
        >
          <SpaceBetween size="m">
            {auth.error ? (
              <Alert type="error" header="Sign-in failed">
                {auth.error.message}
              </Alert>
            ) : null}
            <Box>Sign in with one of the workshop personas through Amazon Cognito</Box>
            {busy ? (
              <Box>
                <Spinner /> Redirecting...
              </Box>
            ) : (
              <Box margin={{ top: "m", bottom: "m" }}>
                <Button variant="primary" fullWidth iconSvg={AWS_LOGO} onClick={signIn} data-testid="sign-in" style={SIGN_IN_BUTTON_STYLE}>
                  Amazon Cognito
                </Button>
              </Box>
            )}
          </SpaceBetween>
        </Container>
      </div>
    </div>
  );
}
