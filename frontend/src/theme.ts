import { applyTheme, type Theme } from "@cloudscape-design/components/theming";

/**
 * AWS Orange #FF9900 for primary buttons, console black #232F3E for the top navigation, Cloudscape defaults otherwise
 * (ui-design.md). Contrast decision (Phase 2B step 2, axe pass of 9 Oct 2026): primary buttons keep dark text #16191F on the
 * orange (8.2:1; white on #FF9900 is 2.1:1) and the hover shade #EC7211 (5.9:1 with the dark text). The step-1 orange accent
 * for links and the active side-navigation entry (#EC7211 on white) measured 3.0:1 (axe "serious", AA needs 4.5:1 for 14 px
 * text), so `colorTextAccent` / `colorTextLinkHover` stay at Cloudscape's validated defaults; orange text appears only on the
 * dark top bar (hover and accent there: 7.0:1 against #232F3E).
 */
export const AWS_ORANGE = "#FF9900";
export const AWS_ORANGE_HOVER = "#EC7211";
export const AWS_ORANGE_ACTIVE = "#DD6B10";
export const CONSOLE_BLACK = "#232F3E";
export const ON_ORANGE_TEXT = "#16191F";

export const theme: Theme = {
  tokens: {
    colorBackgroundButtonPrimaryDefault: AWS_ORANGE,
    colorBackgroundButtonPrimaryHover: AWS_ORANGE_HOVER,
    colorBackgroundButtonPrimaryActive: AWS_ORANGE_ACTIVE,
    colorBorderButtonPrimaryDefault: AWS_ORANGE,
    colorBorderButtonPrimaryHover: AWS_ORANGE_HOVER,
    colorBorderButtonPrimaryActive: AWS_ORANGE_ACTIVE,
    colorTextButtonPrimaryDefault: ON_ORANGE_TEXT,
    colorTextButtonPrimaryHover: ON_ORANGE_TEXT,
    colorTextButtonPrimaryActive: ON_ORANGE_TEXT,
  },
  contexts: {
    "top-navigation": {
      tokens: {
        colorBackgroundContainerContent: CONSOLE_BLACK,
        colorTextTopNavigationTitle: "#FFFFFF",
        colorTextInteractiveDefault: "#D5DBDB",
        colorTextInteractiveHover: AWS_ORANGE,
        colorTextAccent: AWS_ORANGE,
      },
    },
  },
};

export function applyAppTheme(): void {
  applyTheme({ theme });
}
