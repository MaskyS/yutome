import { redirect } from "react-router";

import type { Route } from "./+types/auth.google.callback";
import { buildSessionCookie } from "~/lib/cookies.server";
import { getEnv } from "~/lib/env.server";
import { completeGoogleSignIn, HostedApiError } from "~/lib/hosted-api.server";
import { safeNextPath } from "~/lib/redirect";

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const url = new URL(request.url);
  const error = url.searchParams.get("error");
  if (error) {
    return redirect("/signup?error=google_denied");
  }
  const code = url.searchParams.get("code") ?? "";
  const state = url.searchParams.get("state") ?? "";
  if (!code || !state) {
    return redirect("/signup?error=google_invalid");
  }
  try {
    const { session, redirect_path } = await completeGoogleSignIn(env, {
      code,
      state,
      redirect_uri: `${url.origin}/auth/google/callback`,
    });
    return redirect(safeNextPath(redirect_path) ?? "/dashboard", {
      headers: {
        "set-cookie": buildSessionCookie(session.token, {
          domain: env.YUTOME_COOKIE_DOMAIN,
          maxAgeSeconds: session.max_age_seconds,
        }),
      },
    });
  } catch (error) {
    if (error instanceof HostedApiError) {
      const reason = error.status === 401 ? "google_invalid" : "google_unavailable";
      return redirect(`/signup?error=${reason}`);
    }
    throw error;
  }
}

export default function GoogleCallback() {
  return (
    <main className="text-muted-foreground mx-auto flex min-h-svh max-w-md flex-col justify-center px-4 py-12 text-center text-sm">
      Signing you in…
    </main>
  );
}
