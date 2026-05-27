import { redirect } from "react-router";

import type { Route } from "./+types/auth.verify";
import { buildSessionCookie } from "~/lib/cookies.server";
import { getEnv } from "~/lib/env.server";
import { HostedApiError, verifyLogin } from "~/lib/hosted-api.server";

function safeNextPath(value: string | null): string | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (!trimmed.startsWith("/") || trimmed.startsWith("//")) return null;
  return trimmed;
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
      return redirect("/signup?error=link_invalid");
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
