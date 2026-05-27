import { redirect } from "react-router";

import type { Route } from "./+types/dashboard.youtube.start";
import { getEnv } from "~/lib/env.server";
import { HostedApiError, startYoutubeAuthorization } from "~/lib/hosted-api.server";
import { requireSessionToken, signupRedirect } from "~/lib/session.server";

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const token = requireSessionToken(request);
  const redirectUri = new URL("/dashboard/youtube/callback", request.url).toString();
  try {
    const result = await startYoutubeAuthorization(env, token, redirectUri);
    throw redirect(result.authorization_url);
  } catch (error) {
    if (error instanceof Response) {
      throw error;
    }
    if (error instanceof HostedApiError && error.code.startsWith("account_session")) {
      signupRedirect(env);
    }
    const code = error instanceof HostedApiError ? error.code : "youtube_oauth_failed";
    throw redirect(`/dashboard?youtube=${encodeURIComponent(code)}`);
  }
}
