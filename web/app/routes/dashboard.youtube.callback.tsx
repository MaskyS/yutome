import { redirect } from "react-router";

import type { Route } from "./+types/dashboard.youtube.callback";
import { getEnv } from "~/lib/env.server";
import { completeYoutubeAuthorization, HostedApiError } from "~/lib/hosted-api.server";
import { requireSessionToken, signupRedirect } from "~/lib/session.server";

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const token = requireSessionToken(request);
  const url = new URL(request.url);
  const error = url.searchParams.get("error");
  if (error) {
    throw redirect(`/dashboard?youtube=${encodeURIComponent(error)}`);
  }
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  if (!code || !state) {
    throw redirect("/dashboard?youtube=youtube_oauth_missing_code");
  }
  try {
    await completeYoutubeAuthorization(env, token, {
      code,
      state,
      redirect_uri: new URL("/dashboard/youtube/callback", request.url).toString(),
    });
    throw redirect("/dashboard?youtube=connected");
  } catch (caught) {
    if (caught instanceof Response) {
      throw caught;
    }
    if (caught instanceof HostedApiError && caught.code.startsWith("account_session")) {
      signupRedirect(env);
    }
    const code = caught instanceof HostedApiError ? caught.code : "youtube_oauth_failed";
    throw redirect(`/dashboard?youtube=${encodeURIComponent(code)}`);
  }
}
