const DEFAULT_API_BASE_URL = "/api/v1";

function hasControlCharacter(value: string): boolean {
  return [...value].some((character) => {
    const code = character.charCodeAt(0);
    return code < 32 || (code >= 127 && code <= 159);
  });
}

function isLoopbackHost(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/\.$/, "");
  return normalized === "localhost" || normalized === "127.0.0.1" || normalized === "[::1]";
}

/**
 * Validate the API origin before it can be combined with authenticated paths.
 * Production bundles may use a same-origin path or an explicitly loopback
 * origin only; a remote Vite environment must fail closed before any request.
 */
export function normalizeApiBaseUrl(value: string | undefined, production: boolean): string {
  const candidate = value === undefined ? DEFAULT_API_BASE_URL : value.trim();
  if (!candidate || hasControlCharacter(candidate) || candidate.includes("\\")) {
    throw new Error("The Cortex API base URL is invalid.");
  }

  if (candidate.startsWith("/")) {
    if (candidate.startsWith("//") || candidate.includes("?") || candidate.includes("#")) {
      throw new Error("The Cortex API base URL must be a path without a query or fragment.");
    }
    return candidate.replace(/\/$/, "");
  }

  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new Error("The Cortex API base URL is invalid.");
  }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("The Cortex API base URL is invalid.");
  }
  if (production && !isLoopbackHost(parsed.hostname)) {
    throw new Error("The Cortex API base URL must be relative or loopback-only.");
  }
  return candidate.replace(/\/$/, "");
}

export { DEFAULT_API_BASE_URL };
