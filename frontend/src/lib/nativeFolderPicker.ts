/** The same Windows pywebview folder chooser used by Project workspace. */
type NativeFolderApi = {
  choose_workspace_directory?: () => Promise<string | null>;
};

export async function chooseWorkspaceDirectory(): Promise<string | null> {
  const native = (window as Window & { pywebview?: { api?: NativeFolderApi } }).pywebview?.api;
  if (!native?.choose_workspace_directory) {
    throw new Error("The native folder picker is not available in this browser. Enter an absolute path.");
  }
  return native.choose_workspace_directory();
}
