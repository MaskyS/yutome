import { Form, Link, Outlet } from "react-router";

import type { Route } from "./+types/dashboard";
import { getEnv } from "~/lib/env.server";
import { getSummary } from "~/lib/hosted-api.server";
import { isUnauthorized, requireSessionToken, signupRedirect } from "~/lib/session.server";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const token = requireSessionToken(request);
  try {
    const summary = await getSummary(env, token);
    return { summary };
  } catch (error) {
    if (isUnauthorized(error)) {
      signupRedirect(env);
    }
    throw error;
  }
}

export default function DashboardLayout({ loaderData }: Route.ComponentProps) {
  const { summary } = loaderData;
  return (
    <div className="min-h-svh">
      <header className="border-b">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-4 py-3">
          <div className="flex items-center gap-3">
            <Link to="/dashboard" className="font-semibold">
              Yutome
            </Link>
            {summary.plan_key ? <Badge variant="secondary">{summary.plan_key}</Badge> : null}
          </div>
          <nav className="flex items-center gap-4 text-sm">
            <Link to="/dashboard" className="hover:underline">
              Overview
            </Link>
            <Link to="/dashboard/search" className="hover:underline">
              Search
            </Link>
            <Link to="/dashboard/connect" className="hover:underline">
              Connect
            </Link>
            <span className="text-muted-foreground hidden sm:inline">
              {summary.workspace.name ?? summary.workspace.id}
            </span>
            <Form method="post" action="/signout">
              <Button type="submit" variant="ghost" size="sm">
                Sign out
              </Button>
            </Form>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-8">
        <Outlet />
      </main>
    </div>
  );
}
