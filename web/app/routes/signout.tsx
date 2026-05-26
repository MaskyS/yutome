import { redirect } from "react-router";

import type { Route } from "./+types/signout";
import { clearSessionCookie } from "~/lib/cookies.server";
import { getEnv } from "~/lib/env.server";

export async function action({ context }: Route.ActionArgs) {
  const env = getEnv(context);
  return redirect("/", {
    headers: { "set-cookie": clearSessionCookie(env.YUTOME_COOKIE_DOMAIN) },
  });
}

export async function loader() {
  return redirect("/");
}
