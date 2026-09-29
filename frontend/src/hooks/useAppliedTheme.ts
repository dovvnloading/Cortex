import { useEffect } from "react";
import { applyThemePreference, watchSystemTheme, type ThemePreference } from "../lib/theme";

/**
 * Paint the theme for a saved preference, and keep a "system" preference in
 * step with the operating system.
 *
 * `null` means settings have not loaded yet: do nothing, so the cached theme
 * that was painted before first render stays on screen instead of being
 * replaced by a default.
 */
export function useAppliedTheme(preference: ThemePreference | null): void {
  useEffect(() => {
    if (preference === null) return undefined;
    applyThemePreference(preference);
    if (preference !== "system") return undefined;
    return watchSystemTheme(() => {
      applyThemePreference("system");
    });
  }, [preference]);
}
