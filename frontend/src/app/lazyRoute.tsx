import { lazy, type ComponentType } from "react";

/**
 * A lazily loaded route whose code can be fetched again.
 *
 * React.lazy remembers a rejected import for good, so a chunk that failed once
 * -- typically because an update replaced the files under a window that was
 * left open -- would fail again on every render, and a route boundary's "Try
 * again" would only redraw the same error. `reload()` swaps in a fresh lazy
 * component, so the next render imports the code again. The returned component
 * keeps one identity, so nothing remounts until that happens.
 *
 * Whether a second import can succeed is up to the browser. If it cannot, the
 * boundary still offers a full reload.
 */
export function lazyRoute<Props extends object>(
  load: () => Promise<{ default: ComponentType<Props> }>,
) {
  let Loaded = lazy(load);
  function Route(props: Props) {
    return <Loaded {...(props as Props & React.JSX.IntrinsicAttributes)} />;
  }
  return Object.assign(Route, {
    reload: () => {
      Loaded = lazy(load);
    },
  });
}
