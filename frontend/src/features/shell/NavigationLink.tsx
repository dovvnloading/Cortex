import type { AnchorHTMLAttributes, MouseEvent } from "react";
import { navigate } from "../../lib/navigation";

type Props = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & {
  to: string;
  replace?: boolean;
};

export function NavigationLink({
  to,
  replace = false,
  onClick,
  ...props
}: Props) {
  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    // Modified clicks belong to the browser (new tab/window, context action).
    // Do not run same-tab navigation hooks before checking modifiers: Settings
    // uses one to remember the route it should return to.
    if (
      event.defaultPrevented
      || event.button !== 0
      || event.metaKey
      || event.ctrlKey
      || event.shiftKey
      || event.altKey
      || event.currentTarget.target === "_blank"
    ) {
      return;
    }

    onClick?.(event);
    if (event.defaultPrevented) return;
    event.preventDefault();
    navigate(to, { replace });
  };

  return <a {...props} href={to} onClick={handleClick} />;
}
