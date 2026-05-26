import { Form, redirect, useActionData, useNavigation } from "react-router";

import type { Route } from "./+types/signup";
import { buildSessionCookie } from "~/lib/cookies.server";
import { getEnv } from "~/lib/env.server";
import { bootstrapAccount, HostedApiError } from "~/lib/hosted-api.server";
import { Alert, AlertDescription, AlertTitle } from "~/components/ui/alert";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Input } from "~/components/ui/input";
import { Label } from "~/components/ui/label";

export function meta(_: Route.MetaArgs) {
  return [{ title: "Create your Yutome account" }];
}

export async function action({ request, context }: Route.ActionArgs) {
  const env = getEnv(context);
  const form = await request.formData();
  const email = String(form.get("email") ?? "").trim();
  const name = String(form.get("name") ?? "").trim();
  const workspaceName = String(form.get("workspace_name") ?? "").trim();
  if (!email || !email.includes("@")) {
    return { error: "Enter a valid email address." };
  }
  try {
    const result = await bootstrapAccount(env, {
      email,
      name: name || undefined,
      workspace_name: workspaceName || undefined,
    });
    return redirect("/dashboard", {
      headers: {
        "set-cookie": buildSessionCookie(result.session.token, {
          domain: env.YUTOME_COOKIE_DOMAIN,
          maxAgeSeconds: result.session.max_age_seconds,
        }),
      },
    });
  } catch (error) {
    const message =
      error instanceof HostedApiError ? error.message : "Could not create your account. Please try again.";
    return { error: message };
  }
}

export default function Signup() {
  const actionData = useActionData<typeof action>();
  const navigation = useNavigation();
  const busy = navigation.state !== "idle";
  return (
    <main className="mx-auto flex min-h-svh max-w-md flex-col justify-center px-4 py-12">
      <Card>
        <CardHeader>
          <CardTitle>Create your Yutome account</CardTitle>
          <CardDescription>Set up a hosted workspace, then connect Claude or ChatGPT.</CardDescription>
        </CardHeader>
        <CardContent>
          {actionData?.error ? (
            <Alert variant="destructive" className="mb-4">
              <AlertTitle>Something went wrong</AlertTitle>
              <AlertDescription>{actionData.error}</AlertDescription>
            </Alert>
          ) : null}
          <Form method="post" className="grid gap-4">
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
              {busy ? "Creating…" : "Continue"}
            </Button>
          </Form>
        </CardContent>
      </Card>
      <p className="text-muted-foreground mt-4 text-center text-sm">
        No password needed for now — your email creates and signs you into your workspace.
      </p>
    </main>
  );
}
