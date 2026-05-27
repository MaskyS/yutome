import { redirect } from "react-router";

import type { Route } from "./+types/auth.google.start";
import { getEnv } from "~/lib/env.server";
import { HostedApiError, startGoogleSignIn } from "~/lib/hosted-api.server";
import { safeNextPath } from "~/lib/redirect";

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const url = new URL(request.url);
  const redirectUri = `${url.origin}/auth/google/callback`;
  try {
    const result = await startGoogleSignIn(env, {
      redirect_uri: redirectUri,
      redirect_path: safeNextPath(url.searchParams.get("next")),
    });
    return redirect(result.authorization_url);
  } catch (error) {
    if (error instanceof HostedApiError) {
      return redirect("/signup?error=google_unavailable");
    }
    throw error;
  }
}

export default function GoogleStart() {
  return (
    <main className="text-muted-foreground mx-auto flex min-h-svh max-w-md flex-col justify-center px-4 py-12 text-center text-sm">
      Redirecting to Google…
    </main>
  );
}
