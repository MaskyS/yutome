const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;
const ENCODED_SLASH_OR_BACKSLASH = /%(?:2f|5c)/i;
const INTERNAL_URL_BASE = "https://app.yutome.invalid";

export function safeNextPath(value: string | null | undefined): string | null {
  const trimmed = value?.trim() ?? "";
  if (
    !trimmed ||
    CONTROL_CHARACTERS.test(trimmed) ||
    trimmed.includes("\\") ||
    !trimmed.startsWith("/") ||
    trimmed.startsWith("//")
  ) {
    return null;
  }
  try {
    const parsed = new URL(trimmed, INTERNAL_URL_BASE);
    if (
      parsed.origin !== INTERNAL_URL_BASE ||
      !parsed.pathname.startsWith("/") ||
      parsed.pathname.startsWith("//") ||
      ENCODED_SLASH_OR_BACKSLASH.test(parsed.pathname)
    ) {
      return null;
    }
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return null;
  }
}
