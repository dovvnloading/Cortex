/* eslint-disable react-refresh/only-export-components */
import { Popover as BasePopover } from "@base-ui/react/popover";
import type { ComponentProps, ReactNode } from "react";

export const Popover = {
  Root: BasePopover.Root,
  Trigger: BasePopover.Trigger,
};

type PopoverContentProps = ComponentProps<typeof BasePopover.Popup> & { children: ReactNode };

/** Thin wrapper around Base UI's Portal/Positioner/Popup composition so callers only think in Root/Trigger/Content. */
export function PopoverContent({ children, ...props }: PopoverContentProps) {
  return (
    <BasePopover.Portal>
      <BasePopover.Positioner sideOffset={8} align="end">
        <BasePopover.Popup {...props}>{children}</BasePopover.Popup>
      </BasePopover.Positioner>
    </BasePopover.Portal>
  );
}
