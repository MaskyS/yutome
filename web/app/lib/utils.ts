import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** Format a millisecond offset as a `m:ss` (or `h:mm:ss`) timestamp. */
export function formatTimestamp(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return "0:00";
  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const mm = hours > 0 ? String(minutes).padStart(2, "0") : String(minutes);
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

// Date/time formatting is pinned to a fixed locale + UTC so server-rendered HTML
// matches client hydration (the worker runs UTC; browsers run the user's locale
// and timezone). Without this, dates near midnight render a different calendar
// day on each side and React throws a hydration mismatch.
const DATE_FORMAT = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", year: "numeric", month: "numeric", day: "numeric" });
const CLOCK_FORMAT = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", hour: "2-digit", minute: "2-digit", hour12: false });

/** Deterministic UTC calendar date (e.g. `5/21/2014`). Empty string for invalid input. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : DATE_FORMAT.format(date);
}

/** Deterministic UTC clock time (e.g. `17:49`). Empty string for invalid input. */
export function formatClockTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : `${CLOCK_FORMAT.format(date)} UTC`;
}

/** Whole minutes from a seconds count, locale-grouped (e.g. `1,080s` → `18`). */
export function formatMinutes(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "0";
  return Math.round(seconds / 60).toLocaleString();
}

/** Gigabytes from a byte count: 2 decimals under 10 GB, whole numbers above. */
export function formatGB(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return "0";
  const gb = bytes / 1_073_741_824;
  return gb >= 10 ? Math.round(gb).toLocaleString() : gb.toFixed(2);
}
