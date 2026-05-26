import type { Route } from "./+types/dashboard.connect";
import { getEnv } from "~/lib/env.server";
import { getAssistants } from "~/lib/hosted-api.server";
import { isUnauthorized, requireSessionToken, signupRedirect } from "~/lib/session.server";
import { Card, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { ConnectGuides } from "~/components/connect-guides";
import { CopyField } from "~/components/copy-field";

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const token = requireSessionToken(request);
  try {
    const assistants = await getAssistants(env, token);
    return { assistants, mcpUrl: env.YUTOME_MCP_URL };
  } catch (error) {
    if (isUnauthorized(error)) {
      signupRedirect(env);
    }
    throw error;
  }
}

export default function DashboardConnect({ loaderData }: Route.ComponentProps) {
  const { assistants, mcpUrl } = loaderData;
  return (
    <div className="grid gap-8">
      <section className="grid gap-3">
        <h1 className="text-xl font-semibold">Connect an assistant</h1>
        <p className="text-muted-foreground text-sm">
          Your personal MCP endpoint — paste it into any MCP-capable assistant, then approve access in the
          browser.
        </p>
        <CopyField value={mcpUrl} />
        <ConnectGuides mcpUrl={mcpUrl} />
      </section>

      <section className="grid gap-3">
        <h2 className="text-lg font-semibold">Connected assistants</h2>
        {assistants.length ? (
          <div className="grid gap-2">
            {assistants.map((assistant) => (
              <Card key={assistant.grant_id}>
                <CardHeader>
                  <CardTitle className="text-base">{assistant.client_id ?? "Assistant"}</CardTitle>
                  <CardDescription>
                    {(assistant.scopes.join(", ") || "no scopes") + " · " + assistant.status}
                  </CardDescription>
                </CardHeader>
              </Card>
            ))}
          </div>
        ) : (
          <p className="text-muted-foreground text-sm">
            No assistants connected yet — add the connector above to get started.
          </p>
        )}
      </section>
    </div>
  );
}
