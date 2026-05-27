import { redirect } from "react-router";

import type { Route } from "./+types/auth.verify";
import { buildSessionCookie } from "~/lib/cookies.server";
import { getEnv } from "~/lib/env.server";
import { HostedApiError, verifyLogin } from "~/lib/hosted-api.server";

const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;
const ENCODED_SLASH_OR_BACKSLASH = /%(?:2f|5c)/i;
const INTERNAL_URL_BASE = "https://app.yutome.invalid";

function safeNextPath(value: string | null): string | null {
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

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const url = new URL(request.url);
  const token = url.searchParams.get("token") ?? "";
  if (!token) {
    return redirect("/signup?error=missing_token");
  }
  try {
    const { session, redirect_path } = await verifyLogin(env, token);
    const destination = safeNextPath(redirect_path) ?? "/dashboard";
    return redirect(destination, {
      headers: {
        "set-cookie": buildSessionCookie(session.token, {
          domain: env.YUTOME_COOKIE_DOMAIN,
          maxAgeSeconds: session.max_age_seconds,
        }),
      },
    });
  } catch (error) {
    if (error instanceof HostedApiError) {
      const reason = error.status === 401 ? "link_invalid" : "service_unavailable";
      return redirect(`/signup?error=${reason}`);
    }
    throw error;
  }
}

export default function AuthVerify() {
  return (
    <main className="text-muted-foreground mx-auto flex min-h-svh max-w-md flex-col justify-center px-4 py-12 text-center text-sm">
      Signing you in…
    </main>
  );
}
