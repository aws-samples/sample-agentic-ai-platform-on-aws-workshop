import Alert from "@cloudscape-design/components/alert";
import Button from "@cloudscape-design/components/button";
import { useNavigate } from "react-router-dom";

import { useBreadcrumbs } from "./breadcrumbs";

export function NotFoundPage() {
  const navigate = useNavigate();
  useBreadcrumbs([{ text: "Not found", href: window.location.pathname }]);
  return (
    <Alert type="warning" header="Page not found" action={<Button onClick={() => navigate("/")}>Go to Overview</Button>}>
      There is nothing at {window.location.pathname}.
    </Alert>
  );
}
