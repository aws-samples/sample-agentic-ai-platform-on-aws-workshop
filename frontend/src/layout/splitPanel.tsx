import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

interface SplitPanelValue {
  panel: ReactNode | null;
  open: boolean;
  setPanel: (panel: ReactNode | null) => void;
  setOpen: (open: boolean) => void;
}

const SplitPanelContext = createContext<SplitPanelValue | null>(null);

/** The AppLayout split panel is owned by the shell; pages hand it content (the Catalogue record detail). */
export function SplitPanelProvider({ children }: { children: ReactNode }) {
  const [panel, setPanel] = useState<ReactNode | null>(null);
  const [open, setOpen] = useState(false);
  const value = useMemo(() => ({ panel, open, setPanel, setOpen }), [panel, open]);
  return <SplitPanelContext.Provider value={value}>{children}</SplitPanelContext.Provider>;
}

export function useSplitPanel(): SplitPanelValue {
  const value = useContext(SplitPanelContext);
  if (!value) {
    throw new Error("useSplitPanel must be used inside SplitPanelProvider");
  }
  return value;
}

/** Register split panel content for the lifetime of the calling page. */
export function usePageSplitPanel(panel: ReactNode | null, openWhen: boolean) {
  const { setPanel, setOpen } = useSplitPanel();
  useEffect(() => {
    setPanel(panel);
    return () => setPanel(null);
  }, [panel, setPanel]);
  useEffect(() => {
    if (openWhen) {
      setOpen(true);
    }
  }, [openWhen, setOpen]);
}
