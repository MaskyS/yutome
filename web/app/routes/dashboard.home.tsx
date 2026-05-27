import { useEffect } from "react";
import { Plus, RefreshCcw } from "lucide-react";
import { useFetcher, useRevalidator, useRouteLoaderData } from "react-router";

import type { Route } from "./+types/dashboard.home";
import { getEnv } from "~/lib/env.server";
import {
  createSources,
  getLibrary,
  getSourceJobs,
  HostedApiError,
  type SourceJob,
  type WorkspaceSummary,
} from "~/lib/hosted-api.server";
import { isUnauthorized, requireSessionToken, signupRedirect } from "~/lib/session.server";
import { formatClockTime, formatDate } from "~/lib/utils";
import { Alert, AlertDescription } from "~/components/ui/alert";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Input } from "~/components/ui/input";
import { Label } from "~/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "~/components/ui/table";

export function meta(_: Route.MetaArgs) {
  return [{ title: "Dashboard · Yutome" }];
}

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const token = requireSessionToken(request);
  try {
    const [library, jobs] = await Promise.all([getLibrary(env, token), getSourceJobs(env, token)]);
    return { library, jobs };
  } catch (error) {
    if (isUnauthorized(error)) {
      signupRedirect(env);
    }
    throw error;
  }
}

export async function action({ request, context }: Route.ActionArgs) {
  const env = getEnv(context);
  const token = requireSessionToken(request);
  const form = await request.formData();
  const sourceUrl = String(form.get("source_url") ?? "").trim();
  const refreshEnabled = form.get("refresh_enabled") === "on";
  if (!sourceUrl) {
    return { ok: false, error: "Enter a YouTube video, playlist, channel, or handle." };
  }
  try {
    const result = await createSources(env, token, {
      sources: [
        {
          source_url: sourceUrl,
          selected: true,
          import_source: "manual_url",
        },
      ],
      refresh_enabled: refreshEnabled,
      cadence_seconds: 900,
      max_new_videos: 25,
    });
    return {
      ok: true,
      imported: result.imported.length,
      jobs: result.jobs.length,
    };
  } catch (error) {
    if (isUnauthorized(error)) {
      signupRedirect(env);
    }
    const message = error instanceof HostedApiError ? error.message : "Could not add this source.";
    return { ok: false, error: message };
  }
}

function formatUnit(value: number | string | null): string {
  if (value === null) return "-";
  const numeric = typeof value === "string" ? Number(value) : value;
  return Number.isFinite(numeric) ? numeric.toLocaleString() : String(value);
}

const ACTIVE_JOB_STATUSES = new Set([
  "queued",
  "preparing",
  "discovering",
  "queued_video_jobs",
  "cleaning",
  "embedding",
  "writing_index",
  "retry_wait",
]);

function isActiveJob(job: SourceJob): boolean {
  return ACTIVE_JOB_STATUSES.has(job.status);
}

function statusVariant(status: string): "default" | "secondary" | "destructive" | "outline" {
  if (status === "succeeded") return "outline";
  if (status === "failed" || status === "denied" || status === "cancelled") return "destructive";
  if (ACTIVE_JOB_STATUSES.has(status)) return "secondary";
  return "outline";
}

function jobLabel(job: SourceJob): string {
  const videoId = job.metadata.youtube_video_id;
  if (typeof videoId === "string" && videoId) return videoId;
  const sourceType = job.metadata.source_type;
  if (typeof sourceType === "string" && sourceType) return sourceType;
  return job.source_id ?? "source";
}

export default function DashboardHome({ loaderData }: Route.ComponentProps) {
  const { library, jobs } = loaderData;
  const fetcher = useFetcher<typeof action>();
  const revalidator = useRevalidator();
  const parent = useRouteLoaderData("routes/dashboard") as { summary: WorkspaceSummary } | undefined;
  const summary = parent?.summary;
  const actionData = fetcher.data;
  const busy = fetcher.state !== "idle";
  const hasActiveJobs = jobs.some(isActiveJob);

  useEffect(() => {
    if (actionData?.ok) {
      revalidator.revalidate();
    }
  }, [actionData, revalidator]);

  useEffect(() => {
    if (!hasActiveJobs) return;
    const interval = window.setInterval(() => revalidator.revalidate(), 5000);
    return () => window.clearInterval(interval);
  }, [hasActiveJobs, revalidator]);

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

      <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(360px,0.8fr)]">
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Add source</CardTitle>
            <CardDescription>Video, playlist, channel, or handle</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            {actionData?.ok === false ? (
              <Alert variant="destructive">
                <AlertDescription>{actionData.error}</AlertDescription>
              </Alert>
            ) : null}
            {actionData?.ok ? (
              <Alert>
                <AlertDescription>
                  Added {actionData.imported} source{actionData.imported === 1 ? "" : "s"} and queued{" "}
                  {actionData.jobs} job{actionData.jobs === 1 ? "" : "s"}.
                </AlertDescription>
              </Alert>
            ) : null}
            <fetcher.Form method="post" className="grid gap-3">
              <div className="grid gap-2">
                <Label htmlFor="source_url">YouTube source</Label>
                <Input
                  id="source_url"
                  name="source_url"
                  placeholder="https://www.youtube.com/watch?v=... or @handle"
                  autoComplete="off"
                  required
                />
              </div>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <Label htmlFor="refresh_enabled" className="text-muted-foreground">
                  <input
                    id="refresh_enabled"
                    name="refresh_enabled"
                    type="checkbox"
                    defaultChecked
                    className="accent-primary size-4"
                  />
                  Keep updated
                </Label>
                <Button type="submit" disabled={busy}>
                  {busy ? <RefreshCcw className="animate-spin" /> : <Plus />}
                  {busy ? "Adding" : "Add"}
                </Button>
              </div>
            </fetcher.Form>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Indexing jobs</CardTitle>
            <CardDescription>{hasActiveJobs ? "Refreshing every few seconds" : "Recent activity"}</CardDescription>
          </CardHeader>
          <CardContent>
            {jobs.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Job</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="text-right">Created</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {jobs.slice(0, 8).map((job) => (
                    <TableRow key={job.job_id}>
                      <TableCell>
                        <div className="grid gap-0.5">
                          <span className="text-sm font-medium">{job.job_type}</span>
                          <span className="text-muted-foreground max-w-[18rem] truncate text-xs">{jobLabel(job)}</span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={statusVariant(job.status)}>{job.status}</Badge>
                      </TableCell>
                      <TableCell className="text-muted-foreground text-right text-sm">
                        {job.created_at ? formatClockTime(job.created_at) : "-"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <p className="text-muted-foreground text-sm">No jobs yet.</p>
            )}
          </CardContent>
        </Card>
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
                      <TableCell className="text-muted-foreground">{video.channel_id ?? "-"}</TableCell>
                      <TableCell className="text-muted-foreground text-right">
                        {video.published_at ? formatDate(video.published_at) : "-"}
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
