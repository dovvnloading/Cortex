/**
 * The theme, applied to the document.
 *
 * The saved preference lives in exactly one place: `settings.appearance.theme`
 * in the settings store. Everything here is derived from it and applied to the
 * page, with two extras that exist only so the first paint is right:
 *
 *  - the preference is mirrored to `localStorage` whenever it is applied, and
 *  - the inline script in `index.html` reads that mirror before anything is
 *    drawn and sets `data-theme` and `theme-color` from it.
 *
 * Without the mirror the page could only learn the theme after `/settings`
 * answered, so a light-theme user saw the dark ground on every launch. The
 * mirror is a cache, never a second source of truth: the loaded setting always
 * overwrites it. `theme.test.ts` executes the `index.html` script and checks it
 * resolves the same theme as `applyStoredTheme()` below.
 */

export type ThemePreference = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "cortex.theme";

/** The backend's default appearance, used on a first launch or when the cache is unreadable. */
export const DEFAULT_THEME_PREFERENCE: ThemePreference = "dark";

/**
 * The page ground for each theme. These are the `--bg` values in
 * `styles/tokens.css` and the native window backgrounds in
 * `backend/cortex_backend/launcher/desktop.py`; tests pin all three together so
 * the window, the address-bar colour and the page never disagree.
 */
export const THEME_BACKGROUNDS: Record<ResolvedTheme, string> = {
  dark: "#101112",
  light: "#f3f1ec",
};

const DARK_SCHEME_QUERY = "(prefers-color-scheme: dark)";

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === "light" || value === "dark" || value === "system";
}

const THEME_CYCLE: readonly ThemePreference[] = ["system", "light", "dark"];

/**
 * The preference a "change theme" command moves to: system, then light, then
 * dark, then back to system. "System" is a choice of its own, not a shorthand
 * for dark, so a person who follows Windows is not silently pinned to dark by
 * the first press.
 */
export function nextThemePreference(preference: ThemePreference): ThemePreference {
  return THEME_CYCLE[(THEME_CYCLE.indexOf(preference) + 1) % THEME_CYCLE.length];
}

export const THEME_LABELS: Record<ThemePreference, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
};

export function resolveTheme(preference: ThemePreference, systemPrefersDark: boolean): ResolvedTheme {
  if (preference === "system") return systemPrefersDark ? "dark" : "light";
  return preference;
}

function systemDarkQuery(): MediaQueryList | undefined {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(DARK_SCHEME_QUERY)
    : undefined;
}

/** Storage can be blocked, full or absent (private windows, tests); the theme must not depend on it. */
export function readStoredThemePreference(): ThemePreference | null {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isThemePreference(stored) ? stored : null;
  } catch {
    return null;
  }
}

function writeStoredThemePreference(preference: ThemePreference): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // The cache only speeds up the next launch's first paint.
  }
}

function paintTheme(resolved: ResolvedTheme): void {
  document.documentElement.dataset.theme = resolved;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", THEME_BACKGROUNDS[resolved]);
}

/**
 * Paint the cached preference. This is what runs before settings have loaded
 * (and what the `index.html` script does before the bundle exists); it never
 * writes the cache, since it is only reading it back.
 */
export function applyStoredTheme(): ResolvedTheme {
  const preference = readStoredThemePreference() ?? DEFAULT_THEME_PREFERENCE;
  const resolved = resolveTheme(preference, systemDarkQuery()?.matches ?? false);
  paintTheme(resolved);
  return resolved;
}

/**
 * Apply a preference that came from the settings store: paint it, refresh the
 * cache for the next launch, and tell the native window so its title bar
 * matches a pinned theme that differs from Windows' own.
 */
export function applyThemePreference(preference: ThemePreference): ResolvedTheme {
  const resolved = resolveTheme(preference, systemDarkQuery()?.matches ?? false);
  paintTheme(resolved);
  writeStoredThemePreference(preference);
  syncNativeTitleBar(resolved === "dark");
  return resolved;
}

/** Call `onChange` when the operating system's colour scheme changes. Returns the unsubscribe function. */
export function watchSystemTheme(onChange: () => void): () => void {
  const query = systemDarkQuery();
  if (!query) return () => undefined;
  if (typeof query.addEventListener === "function") {
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }
  // Older WebView builds only have the deprecated pair.
  query.addListener?.(onChange);
  return () => query.removeListener?.(onChange);
}

type NativeWindowBridge = { set_title_bar_dark?: (dark: boolean) => unknown };

function nativeWindowBridge(): NativeWindowBridge | undefined {
  return (window as Window & { pywebview?: { api?: NativeWindowBridge } }).pywebview?.api;
}

let titleBarWaitingForBridge = false;
let latestTitleBarDark = true;

/**
 * Ask the desktop shell to switch its title bar to match the page.
 *
 * Only the native launcher provides `window.pywebview.api.set_title_bar_dark`;
 * in a browser (development, tests) there is no bridge and this does nothing.
 * The bridge is attached after the page loads, which can be after the first
 * theme is applied, so the latest request is replayed once it is ready. It is
 * cosmetic: a missing bridge or a failed call must never affect the app.
 */
export function syncNativeTitleBar(dark: boolean): void {
  latestTitleBarDark = dark;
  const bridge = nativeWindowBridge();
  if (typeof bridge?.set_title_bar_dark === "function") {
    try {
      const result = bridge.set_title_bar_dark(dark);
      if (result instanceof Promise) result.catch(() => undefined);
    } catch {
      // Native chrome is cosmetic.
    }
    return;
  }
  if (titleBarWaitingForBridge) return;
  titleBarWaitingForBridge = true;
  window.addEventListener(
    "pywebviewready",
    () => {
      titleBarWaitingForBridge = false;
      syncNativeTitleBar(latestTitleBarDark);
    },
    { once: true },
  );
}
