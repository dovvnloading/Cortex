/* eslint-disable react-refresh/only-export-components */
import { AlertDialog as BaseAlertDialog } from "@base-ui/react/alert-dialog";
import { Dialog as BaseDialog } from "@base-ui/react/dialog";
import type { ComponentProps, ReactNode } from "react";

/** Regular modal dialog (role="dialog"), e.g. rename. */
export const Dialog = { Root: BaseDialog.Root, Title: BaseDialog.Title, Description: BaseDialog.Description };
/** Destructive-confirmation dialog (role="alertdialog"), e.g. delete. */
export const AlertDialog = { Root: BaseAlertDialog.Root, Title: BaseDialog.Title, Description: BaseDialog.Description };

type DialogContentProps = ComponentProps<typeof BaseDialog.Popup> & { children: ReactNode };

/**
 * Portal + backdrop + popup, shared by both Dialog and AlertDialog — Popup
 * reads its ARIA role ("dialog" vs "alertdialog") from whichever Root
 * (Dialog.Root or AlertDialog.Root) is its nearest ancestor, so one
 * implementation covers both. Base UI also handles the focus trap, focus
 * restoration on close, and Escape-to-close automatically — none of which
 * the previous hand-rolled dialogs implemented for tab-trapping.
 */
export function DialogContent({ children, ...props }: DialogContentProps) {
  return (
    <BaseDialog.Portal>
      <BaseDialog.Backdrop className="dialog-backdrop" />
      <BaseDialog.Popup className="dialog" {...props}>
        {children}
      </BaseDialog.Popup>
    </BaseDialog.Portal>
  );
}
