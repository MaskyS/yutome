import { redirect } from "react-router";

import type { Route } from "./+types/cli.authorize";
import { readSessionCookie } from "~/lib/cookies.server";
import { getEnv } from "~/lib/env.server";
import { authorizeCli } from "~/lib/hosted-api.server";

export function meta(_: Route.MetaArgs) {
  return [{ title: "Authorize Yutome CLI" }];
}

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const url = new URL(request.url);
  const sessionToken = readSessionCookie(request);
  if (!sessionToken) {
    throw redirect(`/signup?next=${encodeURIComponent(url.pathname + url.search)}`);
  }

  const codeChallenge = url.searchParams.get("code_challenge")?.trim();
  const redirectUri = url.searchParams.get("redirect_uri")?.trim();
  const state = url.searchParams.get("state")?.trim() || null;
  if (!codeChallenge || !redirectUri) {
    throw new Response("Missing CLI authorization parameters.", { status: 400 });
  }

  const result = await authorizeCli(env, sessionToken, {
    code_challenge: codeChallenge,
    code_challenge_method: "S256",
    redirect_uri: redirectUri,
    state,
    client_id: url.searchParams.get("client_id")?.trim() || "yutome-cli",
  });
  const callback = new URL(redirectUri);
  callback.searchParams.set("code", result.code);
  if (state) callback.searchParams.set("state", state);
  throw redirect(callback.toString());
}

export default function CliAuthorize() {
  return null;
}

