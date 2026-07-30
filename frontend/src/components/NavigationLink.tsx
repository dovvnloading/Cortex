import type { AnchorHTMLAttributes, MouseEvent } from "react";
import { navigate } from "../lib/navigation";

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
    onClick?.(event);
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

    event.preventDefault();
    navigate(to, { replace });
  };

  return <a {...props} href={to} onClick={handleClick} />;
}
