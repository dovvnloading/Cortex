/* eslint-disable react-refresh/only-export-components */
import { Popover as BasePopover } from "@base-ui/react/popover";
import type { ComponentProps, ReactNode } from "react";

export const Popover = {
  Root: BasePopover.Root,
  Trigger: BasePopover.Trigger,
};

type PositionerProps = ComponentProps<typeof BasePopover.Positioner>;

type PopoverContentProps = ComponentProps<typeof BasePopover.Popup> & {
  children: ReactNode;
  side?: PositionerProps["side"];
  align?: PositionerProps["align"];
  sideOffset?: number;
};

/** Thin wrapper around Base UI's Portal/Positioner/Popup composition so callers only think in Root/Trigger/Content. */
export function PopoverContent({ children, side, align = "end", sideOffset = 8, ...props }: PopoverContentProps) {
  return (
    <BasePopover.Portal>
      <BasePopover.Positioner className="popover-positioner" side={side} align={align} sideOffset={sideOffset} collisionPadding={12}>
        <BasePopover.Popup {...props}>{children}</BasePopover.Popup>
      </BasePopover.Positioner>
    </BasePopover.Portal>
  );
}
