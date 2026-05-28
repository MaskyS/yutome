import { Form, Link, useActionData, useNavigation, useSearchParams } from "react-router";
import { CircleUserRound } from "lucide-react";

import type { Route } from "./+types/signup";
import { getEnv } from "~/lib/env.server";
import { HostedApiError, startLogin } from "~/lib/hosted-api.server";
import { safeNextPath } from "~/lib/redirect";
import { Alert, AlertDescription, AlertTitle } from "~/components/ui/alert";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Input } from "~/components/ui/input";
import { Label } from "~/components/ui/label";

export function meta(_: Route.MetaArgs) {
  return [{ title: "Sign in to Yutome" }];
}

function loginStartErrorMessage(error: unknown): string {
  if (!(error instanceof HostedApiError)) {
    return "Could not send your sign-in link. Please try again.";
  }
  if (error.code === "account_login_invalid_email") {
    return "Enter a valid email address.";
  }
  if (error.status === 502 || error.status === 503) {
    return "Sign-in is temporarily unavailable. Please try again later.";
  }
  return "Could not send your sign-in link. Please try again.";
}

export async function action({ request, context }: Route.ActionArgs) {
  const env = getEnv(context);
  const url = new URL(request.url);
  const form = await request.formData();
  const email = String(form.get("email") ?? "").trim();
  const name = String(form.get("name") ?? "").trim();
  const workspaceName = String(form.get("workspace_name") ?? "").trim();
  const next = safeNextPath(String(form.get("next") ?? url.searchParams.get("next") ?? ""));
  if (!email || !email.includes("@")) {
    return { ok: false as const, error: "Enter a valid email address." };
  }
  try {
    const result = await startLogin(env, {
      email,
      name: name || undefined,
      workspace_name: workspaceName || undefined,
      redirect_path: next || undefined,
    });
    return { ok: true as const, email: result.email, verifyLink: result.verify_link ?? null };
  } catch (error) {
    return { ok: false as const, error: loginStartErrorMessage(error) };
  }
}

export default function Signup() {
  const actionData = useActionData<typeof action>();
  const navigation = useNavigation();
  const [searchParams] = useSearchParams();
  const next = safeNextPath(searchParams.get("next") ?? "");
  const busy = navigation.state !== "idle";
  const linkError =
    searchParams.get("error") === "link_invalid"
      ? "That sign-in link is invalid or has expired. Request a new one below."
      : searchParams.get("error") === "missing_token"
        ? "That sign-in link was incomplete. Request a new one below."
        : searchParams.get("error") === "service_unavailable"
          ? "Sign-in is temporarily unavailable. Request a new link later."
          : searchParams.get("error") === "google_denied"
            ? "Google sign-in was cancelled."
            : searchParams.get("error") === "google_invalid"
              ? "That Google sign-in link is invalid or has expired. Start again below."
              : searchParams.get("error") === "google_unavailable"
                ? "Google sign-in is temporarily unavailable. Use an email link or try again later."
          : null;
  const googleStartPath = `/auth/google/start${next ? `?next=${encodeURIComponent(next)}` : ""}`;

  if (actionData?.ok) {
    return (
      <main className="mx-auto flex min-h-svh max-w-md flex-col justify-center px-4 py-12">
        <Card>
          <CardHeader>
            <CardTitle>Check your email</CardTitle>
            <CardDescription>
              We sent a sign-in link to <span className="font-medium">{actionData.email}</span>. Open it on this device
              to finish signing in. The link expires shortly and works once.
            </CardDescription>
          </CardHeader>
          {actionData.verifyLink ? (
            <CardContent>
              <Alert>
                <AlertTitle>Dev mode</AlertTitle>
                <AlertDescription>
                  Email delivery isn&apos;t configured locally.{" "}
                  <a className="underline" href={actionData.verifyLink}>
                    Use this sign-in link
                  </a>
                  .
                </AlertDescription>
              </Alert>
            </CardContent>
          ) : null}
        </Card>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-svh max-w-md flex-col justify-center px-4 py-12">
      <Card>
        <CardHeader>
          <CardTitle>Sign in to Yutome</CardTitle>
          <CardDescription>
            Enter your email and we&apos;ll send a one-time sign-in link. New here? This also creates your workspace.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {actionData?.ok === false ? (
            <Alert variant="destructive" className="mb-4">
              <AlertTitle>Something went wrong</AlertTitle>
              <AlertDescription>{actionData.error}</AlertDescription>
            </Alert>
          ) : linkError ? (
            <Alert variant="destructive" className="mb-4">
              <AlertDescription>{linkError}</AlertDescription>
            </Alert>
          ) : null}
          <Button asChild variant="outline" className="mb-4 w-full">
            <a href={googleStartPath}>
              <CircleUserRound aria-hidden="true" />
              Sign in with Google
            </a>
          </Button>
          <div className="text-muted-foreground mb-4 flex items-center gap-3 text-xs">
            <div className="bg-border h-px flex-1" />
            <span>or</span>
            <div className="bg-border h-px flex-1" />
          </div>
          <Form method="post" className="grid gap-4">
            {next ? <input type="hidden" name="next" value={next} /> : null}
            <div className="grid gap-2">
              <Label htmlFor="email">Email</Label>
              <Input id="email" name="email" type="email" autoComplete="email" required autoFocus />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="name">Name</Label>
              <Input id="name" name="name" autoComplete="name" />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="workspace_name">Workspace name</Label>
              <Input id="workspace_name" name="workspace_name" autoComplete="organization" />
            </div>
            <Button type="submit" disabled={busy}>
              {busy ? "Sending link…" : "Email me a sign-in link"}
            </Button>
          </Form>
        </CardContent>
      </Card>
      <footer className="text-muted-foreground mt-6 flex justify-center gap-4 text-xs">
        <Link to="/privacy" className="hover:text-foreground">
          Privacy
        </Link>
        <Link to="/terms" className="hover:text-foreground">
          Terms
        </Link>
      </footer>
    </main>
  );
}
