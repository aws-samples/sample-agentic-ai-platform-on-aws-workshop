import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export interface Crumb {
  text: string;
  href: string;
}

interface BreadcrumbsValue {
  crumbs: Crumb[];
  setCrumbs: (crumbs: Crumb[]) => void;
}

const BreadcrumbsContext = createContext<BreadcrumbsValue | null>(null);

export const ROOT_CRUMB: Crumb = { text: "AnyCompany Agent Platform", href: "/" };

export function BreadcrumbsProvider({ children }: { children: ReactNode }) {
  const [crumbs, setCrumbs] = useState<Crumb[]>([ROOT_CRUMB]);
  const value = useMemo(() => ({ crumbs, setCrumbs }), [crumbs]);
  return <BreadcrumbsContext.Provider value={value}>{children}</BreadcrumbsContext.Provider>;
}

export function useBreadcrumbsValue(): BreadcrumbsValue {
  const value = useContext(BreadcrumbsContext);
  if (!value) {
    throw new Error("useBreadcrumbs must be used inside BreadcrumbsProvider");
  }
  return value;
}

/** Pages declare their breadcrumb trail (after the root crumb). */
export function useBreadcrumbs(crumbs: Crumb[]) {
  const { setCrumbs } = useBreadcrumbsValue();
  const key = crumbs.map((crumb) => `${crumb.text}|${crumb.href}`).join("//");
  useEffect(() => {
    setCrumbs([ROOT_CRUMB, ...crumbs]);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `key` captures the crumbs' content
  }, [key, setCrumbs]);
}
