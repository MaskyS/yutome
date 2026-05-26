// Session handling for the dashboard BFF. The RR app does NOT verify the token
// itself — it forwards the cookie to the Python API, which is the authoritative
// verifier (account.verify_account_session_token). A 401 from the API means the
// session is missing/expired → bounce to /signup and clear the stale cookie.
import { redirect } from "react-router";

import { clearSessionCookie, readSessionCookie } from "./cookies.server";
import type { YutomeWebEnv } from "./env.server";
import { HostedApiError } from "./hosted-api.server";

export function requireSessionToken(request: Request): string {
  const token = readSessionCookie(request);
  if (!token) {
    throw redirect("/signup");
  }
  return token;
}

export function isUnauthorized(error: unknown): boolean {
  return error instanceof HostedApiError && error.status === 401;
}

export function signupRedirect(env: YutomeWebEnv): never {
  throw redirect("/signup", {
    headers: { "set-cookie": clearSessionCookie(env.YUTOME_COOKIE_DOMAIN) },
  });
}
