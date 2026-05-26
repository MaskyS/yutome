import { useRouteLoaderData } from "react-router";

import type { Route } from "./+types/dashboard.home";
import { getEnv } from "~/lib/env.server";
import { getLibrary, type WorkspaceSummary } from "~/lib/hosted-api.server";
import { isUnauthorized, requireSessionToken, signupRedirect } from "~/lib/session.server";
import { Badge } from "~/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "~/components/ui/table";

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const token = requireSessionToken(request);
  try {
    const library = await getLibrary(env, token);
    return { library };
  } catch (error) {
    if (isUnauthorized(error)) {
      signupRedirect(env);
    }
    throw error;
  }
}

function formatUnit(value: number | string | null): string {
  if (value === null) return "—";
  const numeric = typeof value === "string" ? Number(value) : value;
  return Number.isFinite(numeric) ? numeric.toLocaleString() : String(value);
}

export default function DashboardHome({ loaderData }: Route.ComponentProps) {
  const { library } = loaderData;
  const parent = useRouteLoaderData("routes/dashboard") as { summary: WorkspaceSummary } | undefined;
  const summary = parent?.summary;
  return (
    <div className="grid gap-8">
      <section className="grid gap-4">
        <h1 className="text-xl font-semibold">Overview</h1>
        <div className="grid gap-4 sm:grid-cols-3">
          <Card>
            <CardHeader className="pb-2">
              <CardDescription>Videos indexed</CardDescription>
              <CardTitle className="text-3xl">{library.counts.videos.toLocaleString()}</CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardDescription>Channels</CardDescription>
              <CardTitle className="text-3xl">{library.counts.channels.toLocaleString()}</CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardDescription>Sources</CardDescription>
              <CardTitle className="text-3xl">{library.counts.sources.toLocaleString()}</CardTitle>
            </CardHeader>
          </Card>
        </div>
      </section>

      {summary ? (
        <section className="grid gap-3">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold">Plan &amp; usage</h2>
            {summary.plan_key ? (
              <Badge variant="secondary">{summary.plan_key}</Badge>
            ) : (
              <Badge variant="outline">no active plan</Badge>
            )}
          </div>
          {summary.units.length ? (
            <Card>
              <CardContent className="pt-6">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Unit</TableHead>
                      <TableHead className="text-right">Included</TableHead>
                      <TableHead className="text-right">Used</TableHead>
                      <TableHead className="text-right">Remaining</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {summary.units.map((unit) => (
                      <TableRow key={unit.unit}>
                        <TableCell className="font-medium">{unit.unit}</TableCell>
                        <TableCell className="text-right">
                          {unit.unlimited ? "Unlimited" : formatUnit(unit.included)}
                        </TableCell>
                        <TableCell className="text-right">{formatUnit(unit.used)}</TableCell>
                        <TableCell className="text-right">
                          {unit.unlimited ? "Unlimited" : formatUnit(unit.remaining)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
          ) : (
            <p className="text-muted-foreground text-sm">No active plan period yet.</p>
          )}
        </section>
      ) : null}

      <section className="grid gap-3">
        <h2 className="text-lg font-semibold">Recent videos</h2>
        {library.recent.length ? (
          <Card>
            <CardContent className="pt-6">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Title</TableHead>
                    <TableHead>Channel</TableHead>
                    <TableHead className="text-right">Published</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {library.recent.map((video) => (
                    <TableRow key={video.video_id}>
                      <TableCell className="font-medium">{video.title ?? video.video_id}</TableCell>
                      <TableCell className="text-muted-foreground">{video.channel_id ?? "—"}</TableCell>
                      <TableCell className="text-muted-foreground text-right">
                        {video.published_at ? new Date(video.published_at).toLocaleDateString() : "—"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        ) : (
          <p className="text-muted-foreground text-sm">Nothing indexed yet.</p>
        )}
      </section>
    </div>
  );
}
