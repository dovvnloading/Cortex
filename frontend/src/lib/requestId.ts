/**
 * The form every request id must take: the API and the stored recovery
 * records accept letters, digits, "_" and "-" only, starting with a letter or
 * digit, at most 128 characters.
 */
export const REQUEST_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;

/**
 * A fresh idempotency key for one request.
 *
 * Where `crypto.randomUUID` is missing (an insecure context, an older
 * WebView), the fallback used to be `${Date.now()}-${Math.random()}`: the
 * decimal point in that number failed the API's pattern, so attachments could
 * not be staged and a lost admission could not be replayed (#696). The
 * fallback is now random bytes in hexadecimal.
 */
export function createRequestId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
