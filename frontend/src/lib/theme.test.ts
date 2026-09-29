import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import indexHtml from "../../index.html?raw";
import { tokensCss } from "../test/css";
import {
  applyStoredTheme,
  applyThemePreference,
  DEFAULT_THEME_PREFERENCE,
  readStoredThemePreference,
  resolveTheme,
  syncNativeTitleBar,
  THEME_BACKGROUNDS,
  THEME_STORAGE_KEY,
  watchSystemTheme,
  type ThemePreference,
} from "./theme";

type Listener = (event: MediaQueryListEvent) => void;

function stubSystemScheme(initiallyDark: boolean) {
  let dark = initiallyDark;
  const listeners = new Set<Listener>();
  const query = {
    get matches() { return dark; },
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_type: "change", listener: Listener) => listeners.add(listener),
    removeEventListener: (_type: "change", listener: Listener) => listeners.delete(listener),
  } as unknown as MediaQueryList;
  vi.stubGlobal("matchMedia", vi.fn(() => query));
  return {
    listeners,
    setDark(next: boolean) {
      dark = next;
      listeners.forEach((listener) => listener({ matches: next } as MediaQueryListEvent));
    },
  };
}

function themeColorMeta(): HTMLMetaElement {
  return document.head.querySelector('meta[name="theme-color"]') as HTMLMetaElement;
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.dataset.theme = "dark";
  document.head.querySelector('meta[name="theme-color"]')?.remove();
  const meta = document.createElement("meta");
  meta.name = "theme-color";
  meta.content = THEME_BACKGROUNDS.dark;
  document.head.append(meta);
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  delete (window as Partial<Window> & { pywebview?: unknown }).pywebview;
});

describe("resolveTheme", () => {
  it.each<[ThemePreference, boolean, "light" | "dark"]>([
    ["light", true, "light"],
    ["light", false, "light"],
    ["dark", true, "dark"],
    ["dark", false, "dark"],
    ["system", true, "dark"],
    ["system", false, "light"],
  ])("%s with the system dark=%s resolves to %s", (preference, systemDark, expected) => {
    expect(resolveTheme(preference, systemDark)).toBe(expected);
  });
});

describe("the stored preference", () => {
  it("reads back only a known preference", () => {
    expect(readStoredThemePreference()).toBeNull();
    for (const value of ["light", "dark", "system"] as const) {
      window.localStorage.setItem(THEME_STORAGE_KEY, value);
      expect(readStoredThemePreference()).toBe(value);
    }
    window.localStorage.setItem(THEME_STORAGE_KEY, "solarized");
    expect(readStoredThemePreference()).toBeNull();
  });

  it("treats blocked storage as no cache rather than throwing", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("blocked", "SecurityError"); });
    expect(readStoredThemePreference()).toBeNull();
    expect(applyStoredTheme()).toBe(DEFAULT_THEME_PREFERENCE);
  });

  it("still applies a preference when storage rejects the write", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("full", "QuotaExceededError"); });
    stubSystemScheme(true);
    expect(() => applyThemePreference("light")).not.toThrow();
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});

describe("applyThemePreference", () => {
  it("paints the document and the address-bar colour and refreshes the cache", () => {
    stubSystemScheme(true);

    expect(applyThemePreference("light")).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(themeColorMeta().content).toBe(THEME_BACKGROUNDS.light);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");

    expect(applyThemePreference("dark")).toBe("dark");
    expect(themeColorMeta().content).toBe(THEME_BACKGROUNDS.dark);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });

  it("caches the preference, not the resolved theme, so system keeps following the OS", () => {
    stubSystemScheme(false);
    applyThemePreference("system");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("system");
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("copes with a browser that has no matchMedia", () => {
    vi.stubGlobal("matchMedia", undefined);
    // Same answer the old in-component code gave: with nothing to ask, system is light.
    expect(applyThemePreference("system")).toBe("light");
    expect(watchSystemTheme(() => undefined)).toBeTypeOf("function");
  });
});

describe("watchSystemTheme", () => {
  it("reports scheme changes until unsubscribed", () => {
    const system = stubSystemScheme(false);
    const onChange = vi.fn();
    const stop = watchSystemTheme(onChange);

    system.setDark(true);
    expect(onChange).toHaveBeenCalledTimes(1);

    stop();
    system.setDark(false);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(system.listeners.size).toBe(0);
  });

  it("falls back to the deprecated listener API on older engines", () => {
    const addListener = vi.fn();
    const removeListener = vi.fn();
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener, removeListener })));
    const onChange = vi.fn();

    const stop = watchSystemTheme(onChange);
    expect(addListener).toHaveBeenCalledWith(onChange);
    stop();
    expect(removeListener).toHaveBeenCalledWith(onChange);
  });
});

describe("the page ground colours", () => {
  const tokenBackground = (selector: RegExp) => selector.exec(tokensCss)?.[1];

  it("equal --bg in tokens.css, so the address bar and the page agree", () => {
    expect(tokenBackground(/:root\s*\{[^}]*?--bg:\s*(#[0-9a-f]{6})/i)).toBe(THEME_BACKGROUNDS.light);
    expect(tokenBackground(/:root\[data-theme="dark"\]\s*\{[^}]*?--bg:\s*(#[0-9a-f]{6})/i)).toBe(THEME_BACKGROUNDS.dark);
  });

  it("are what index.html ships before any script runs", () => {
    expect(indexHtml).toContain(`<meta name="theme-color" content="${THEME_BACKGROUNDS.dark}" />`);
    expect(indexHtml).toContain('<html lang="en" data-theme="dark">');
  });
});

describe("the inline script in index.html", () => {
  // Case-insensitive and tolerant of attributes and a spaced closing tag, so this
  // finds the inline script however the markup is written.
  const source = /<script\b(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script\s*>/i.exec(indexHtml)?.[1];

  const runInlineScript = () => {
    if (!source) throw new Error("index.html has no inline script");
    new Function(source)();
    return { theme: document.documentElement.dataset.theme, color: themeColorMeta().content };
  };

  it("exists and is not the module entry", () => {
    expect(source).toBeTruthy();
    expect(source).not.toContain("import ");
  });

  const cases: Array<[stored: string | null, systemDark: boolean]> = [];
  for (const stored of [null, "light", "dark", "system", "solarized", ""]) {
    for (const systemDark of [true, false]) cases.push([stored, systemDark]);
  }

  it.each(cases)("resolves the same theme as applyStoredTheme() for stored=%j and system dark=%s", (stored, systemDark) => {
    if (stored !== null) window.localStorage.setItem(THEME_STORAGE_KEY, stored);
    stubSystemScheme(systemDark);

    document.documentElement.dataset.theme = "dark";
    themeColorMeta().content = "#000000";
    const shipped = runInlineScript();

    document.documentElement.dataset.theme = "dark";
    themeColorMeta().content = "#000000";
    const resolved = applyStoredTheme();

    expect(shipped).toEqual({ theme: resolved, color: THEME_BACKGROUNDS[resolved] });
    expect(document.documentElement.dataset.theme).toBe(resolved);
    expect(themeColorMeta().content).toBe(THEME_BACKGROUNDS[resolved]);
  });

  it("paints a light ground for a cached light preference (the launch flash)", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "light");
    expect(runInlineScript()).toEqual({ theme: "light", color: THEME_BACKGROUNDS.light });
  });

  it("falls back to dark when storage is blocked and does not throw", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("blocked", "SecurityError"); });
    stubSystemScheme(false);
    expect(runInlineScript()).toEqual({ theme: "dark", color: THEME_BACKGROUNDS.dark });
  });

  it("does not require matchMedia", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "system");
    vi.stubGlobal("matchMedia", undefined);
    expect(runInlineScript().theme).toBe(applyStoredTheme());
  });
});

describe("syncNativeTitleBar", () => {
  it("does nothing in a plain browser", () => {
    expect(() => syncNativeTitleBar(false)).not.toThrow();
  });

  it("calls the launcher's bridge with the resolved darkness when it exists", () => {
    const setTitleBarDark = vi.fn();
    (window as unknown as { pywebview: unknown }).pywebview = { api: { set_title_bar_dark: setTitleBarDark } };
    stubSystemScheme(true);

    applyThemePreference("light");
    applyThemePreference("dark");
    applyThemePreference("system");

    expect(setTitleBarDark.mock.calls).toEqual([[false], [true], [true]]);
  });

  it("never lets a failing or rejecting bridge break theming", async () => {
    const throwing = vi.fn(() => { throw new Error("native window gone"); });
    (window as unknown as { pywebview: unknown }).pywebview = { api: { set_title_bar_dark: throwing } };
    expect(() => applyThemePreference("light")).not.toThrow();
    expect(document.documentElement.dataset.theme).toBe("light");

    const rejecting = vi.fn(() => Promise.reject(new Error("no window")));
    (window as unknown as { pywebview: unknown }).pywebview = { api: { set_title_bar_dark: rejecting } };
    expect(() => applyThemePreference("dark")).not.toThrow();
    await Promise.resolve();
    expect(rejecting).toHaveBeenCalledWith(true);
  });

  it("replays the latest request once the bridge appears", () => {
    // pywebview attaches its API after the page loads, so the first theme can
    // be applied before it exists.
    syncNativeTitleBar(true);
    syncNativeTitleBar(false);

    const setTitleBarDark = vi.fn();
    (window as unknown as { pywebview: unknown }).pywebview = { api: { set_title_bar_dark: setTitleBarDark } };
    window.dispatchEvent(new Event("pywebviewready"));

    expect(setTitleBarDark.mock.calls).toEqual([[false]]);
  });
});
