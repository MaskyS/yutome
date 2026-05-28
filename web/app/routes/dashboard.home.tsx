import { useEffect } from "react";
import { AlertTriangle, Check, CirclePlay, Info, Loader2, Plus, RefreshCcw, Unplug } from "lucide-react";
import { Link, useFetcher, useRevalidator, useRouteLoaderData } from "react-router";

import type { Route } from "./+types/dashboard.home";
import { getEnv } from "~/lib/env.server";
import {
  createSources,
  getAssistants,
  getLibrary,
  getSourceJobs,
  getYoutubeStatus,
  getYoutubeSubscriptions,
  HostedApiError,
  importYoutubeSubscriptions,
  revokeYoutubeConnection,
  type WorkspaceEntitlement,
  type WorkspaceSummary,
  type YoutubeSubscriptionChannel,
} from "~/lib/hosted-api.server";
import { isUnauthorized, requireSessionToken, signupRedirect } from "~/lib/session.server";
import { hasActiveActivity, toActivity, type ActivityStatus } from "~/lib/activity";
import { formatClockTime, formatDate, formatGB, formatMinutes, formatUSD } from "~/lib/utils";
import { Alert, AlertDescription } from "~/components/ui/alert";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Input } from "~/components/ui/input";
import { Label } from "~/components/ui/label";
import { Progress } from "~/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "~/components/ui/table";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "~/components/ui/tooltip";
import { ConnectGuides } from "~/components/connect-guides";
import { CopyField } from "~/components/copy-field";

export function meta(_: Route.MetaArgs) {
  return [{ title: "Dashboard · Yutome" }];
}

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const token = requireSessionToken(request);
  const url = new URL(request.url);
  try {
    // Separate hosted endpoints (each its own request/connection), so parallel is safe;
    // the sequential constraint only applies to the `list` adapter (see frontend.md §6).
    const [library, jobs, assistants, youtube] = await Promise.all([
      getLibrary(env, token),
      getSourceJobs(env, token, 100),
      getAssistants(env, token),
      getYoutubeStatus(env, token),
    ]);
    let youtubeSubscriptions: YoutubeSubscriptionChannel[] = [];
    let youtubeError: string | null = null;
    if (youtube.connected) {
      try {
        youtubeSubscriptions = await getYoutubeSubscriptions(env, token);
      } catch (error) {
        if (isAccountSessionError(error)) {
          signupRedirect(env);
        }
        youtubeError = error instanceof HostedApiError ? error.message : "Could not load YouTube subscriptions.";
      }
    }
    return {
      library,
      jobs,
      assistants,
      youtube,
      youtubeSubscriptions,
      youtubeError,
      youtubeNotice: url.searchParams.get("youtube"),
      mcpUrl: env.YUTOME_MCP_URL,
    };
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
  const intent = String(form.get("_intent") ?? "add_source");
  if (intent === "disconnect_youtube") {
    try {
      const result = await revokeYoutubeConnection(env, token);
      return { ok: true, intent, revoked: result.revoked, imported: 0, jobs: 0 };
    } catch (error) {
      if (isAccountSessionError(error)) {
        signupRedirect(env);
      }
      const message = error instanceof HostedApiError ? error.message : "Could not disconnect YouTube.";
      return { ok: false, intent, error: message };
    }
  }
  if (intent === "import_youtube_subscriptions") {
    const channelIds = form
      .getAll("channel_id")
      .map((value) => String(value).trim())
      .filter(Boolean);
    if (!channelIds.length) {
      return { ok: false, intent, error: "Select at least one YouTube subscription." };
    }
    try {
      const result = await importYoutubeSubscriptions(env, token, {
        channel_ids: channelIds,
        refresh_enabled: form.get("youtube_refresh_enabled") === "on",
        cadence_seconds: 900,
        max_new_videos: 25,
      });
      return {
        ok: true,
        intent,
        imported: result.imported.length,
        jobs: result.jobs.length,
      };
    } catch (error) {
      if (isAccountSessionError(error)) {
        signupRedirect(env);
      }
      const message = error instanceof HostedApiError ? error.message : "Could not import YouTube subscriptions.";
      return { ok: false, intent, error: message };
    }
  }
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
    if (isAccountSessionError(error)) {
      signupRedirect(env);
    }
    const message = error instanceof HostedApiError ? error.message : "Could not add this source.";
    return { ok: false, error: message };
  }
}

function entitlementValue(entitlement: WorkspaceEntitlement): string {
  if (entitlement.unlimited) return "Unlimited";
  const included = entitlement.included;
  switch (entitlement.format) {
    case "minutes":
      return `${formatMinutes(entitlement.used)} / ${included != null ? formatMinutes(included) : "—"} min`;
    case "bytes":
      return `${formatGB(entitlement.used)} / ${included != null ? formatGB(included) : "—"} GB`;
    case "ratio":
      // Per the glossary, never show the raw token count — only a proportion.
      return `${Math.round((entitlement.percent ?? 0) * 100)}% used`;
    case "count":
    default:
      return `${entitlement.used.toLocaleString()} / ${included != null ? included.toLocaleString() : "—"}`;
  }
}

function ActivityIcon({ status }: { status: ActivityStatus }) {
  if (status === "working") return <Loader2 className="text-muted-foreground size-4 animate-spin" />;
  if (status === "failed") return <AlertTriangle className="text-destructive size-4" />;
  return <Check className="size-4 text-emerald-600" />;
}

function isAccountSessionError(error: unknown): boolean {
  return error instanceof HostedApiError && error.code.startsWith("account_session");
}

function youtubeNoticeMessage(code: string | null): string | null {
  if (!code) return null;
  if (code === "connected") return "YouTube connected.";
  if (code === "access_denied") return "YouTube connection was cancelled.";
  if (code === "youtube_oauth_unconfigured") return "YouTube connection is not configured.";
  return "YouTube connection did not finish. Try again.";
}

export default function DashboardHome({ loaderData }: Route.ComponentProps) {
  const { library, jobs, assistants, youtube, youtubeSubscriptions, youtubeError, youtubeNotice, mcpUrl } = loaderData;
  const fetcher = useFetcher<typeof action>();
  const youtubeFetcher = useFetcher<typeof action>();
  const revalidator = useRevalidator();
  const parent = useRouteLoaderData("routes/dashboard") as { summary: WorkspaceSummary } | undefined;
  const summary = parent?.summary;
  const actionData = fetcher.data;
  const youtubeActionData = youtubeFetcher.data;
  const busy = fetcher.state !== "idle";
  const youtubeBusy = youtubeFetcher.state !== "idle";
  const activity = toActivity(jobs);
  const activityRunning = hasActiveActivity(activity);

  useEffect(() => {
    if (actionData?.ok) {
      revalidator.revalidate();
    }
  }, [actionData, revalidator]);

  useEffect(() => {
    if (youtubeActionData?.ok) {
      revalidator.revalidate();
    }
  }, [youtubeActionData, revalidator]);

  useEffect(() => {
    if (!activityRunning) return;
    const interval = window.setInterval(() => revalidator.revalidate(), 5000);
    return () => window.clearInterval(interval);
  }, [activityRunning, revalidator]);

  return (
    <div className="grid gap-8">
      <section className="grid gap-3">
        <h1 className="text-xl font-semibold">Connect your assistant</h1>
        <p className="text-muted-foreground text-sm">
          Your personal MCP endpoint — paste it into any MCP-capable assistant, then approve access in the browser.
        </p>
        <CopyField value={mcpUrl} />
        <ConnectGuides mcpUrl={mcpUrl} />
        {assistants.length ? (
          <div className="grid gap-2">
            <h2 className="text-muted-foreground text-sm font-medium">Connected assistants</h2>
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
            No assistants connected yet — paste the URL above into your assistant to get started.
          </p>
        )}
      </section>

      <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(360px,0.8fr)]">
        <div className="grid gap-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Add a source</CardTitle>
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
                <input type="hidden" name="_intent" value="add_source" />
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
              <div className="text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                <span>{library.counts.videos.toLocaleString()} videos</span>
                <span aria-hidden>·</span>
                <span>{library.counts.channels.toLocaleString()} channels</span>
                <span aria-hidden>·</span>
                <span>{library.counts.sources.toLocaleString()} sources</span>
                <Link to="/dashboard/library" className="text-foreground ml-auto hover:underline">
                  View library →
                </Link>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <div className="flex items-start justify-between gap-3">
                <div className="grid gap-1">
                  <CardTitle className="flex items-center gap-2 text-lg">
                    <CirclePlay className="text-destructive size-5" />
                    YouTube subscriptions
                  </CardTitle>
                  <CardDescription>
                    {youtube.connected
                      ? `${youtubeSubscriptions.length.toLocaleString()} channels available`
                      : "Optional read-only channel picker"}
                  </CardDescription>
                </div>
                {youtube.connected ? <Badge variant="secondary">Connected</Badge> : null}
              </div>
            </CardHeader>
            <CardContent className="grid gap-4">
              {youtubeNoticeMessage(youtubeNotice) ? (
                <Alert>
                  <AlertDescription>{youtubeNoticeMessage(youtubeNotice)}</AlertDescription>
                </Alert>
              ) : null}
              {youtubeError ? (
                <Alert variant="destructive">
                  <AlertDescription>{youtubeError}</AlertDescription>
                </Alert>
              ) : null}
              {youtubeActionData?.ok === false ? (
                <Alert variant="destructive">
                  <AlertDescription>{youtubeActionData.error}</AlertDescription>
                </Alert>
              ) : null}
              {youtubeActionData?.ok ? (
                <Alert>
                  <AlertDescription>
                    {youtubeActionData.intent === "disconnect_youtube"
                      ? "YouTube disconnected."
                      : `Imported ${youtubeActionData.imported} channel${youtubeActionData.imported === 1 ? "" : "s"} and queued ${youtubeActionData.jobs} job${youtubeActionData.jobs === 1 ? "" : "s"}.`}
                  </AlertDescription>
                </Alert>
              ) : null}
              {!youtube.configured ? (
                <Alert variant="destructive">
                  <AlertDescription>YouTube connection is not configured.</AlertDescription>
                </Alert>
              ) : youtube.connected ? (
                <div className="grid gap-3">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-muted-foreground text-sm">
                      {youtube.grant?.connected_at ? `Connected ${formatDate(youtube.grant.connected_at)}` : "Connected"}
                    </span>
                    <div className="flex gap-2">
                      <Button asChild variant="outline" size="sm">
                        <Link to="/dashboard/youtube/start">
                          <CirclePlay />
                          Reconnect
                        </Link>
                      </Button>
                      <youtubeFetcher.Form method="post">
                        <input type="hidden" name="_intent" value="disconnect_youtube" />
                        <Button type="submit" variant="outline" size="sm" disabled={youtubeBusy}>
                          <Unplug />
                          Disconnect
                        </Button>
                      </youtubeFetcher.Form>
                    </div>
                  </div>
                  {youtubeSubscriptions.length ? (
                    <youtubeFetcher.Form method="post" className="grid gap-3">
                      <input type="hidden" name="_intent" value="import_youtube_subscriptions" />
                      <div className="max-h-72 overflow-auto rounded-md border">
                        {youtubeSubscriptions.map((channel) => (
                          <label
                            key={channel.channel_id}
                            className="hover:bg-muted/60 flex min-h-10 items-center gap-3 border-b px-3 py-2 last:border-b-0"
                          >
                            <input
                              name="channel_id"
                              value={channel.channel_id}
                              type="checkbox"
                              className="accent-primary size-4 shrink-0"
                            />
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-sm font-medium">
                                {channel.title ?? channel.channel_id}
                              </span>
                              <span className="text-muted-foreground block truncate text-xs">{channel.channel_id}</span>
                            </span>
                          </label>
                        ))}
                      </div>
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <Label htmlFor="youtube_refresh_enabled" className="text-muted-foreground">
                          <input
                            id="youtube_refresh_enabled"
                            name="youtube_refresh_enabled"
                            type="checkbox"
                            defaultChecked
                            className="accent-primary size-4"
                          />
                          Keep updated
                        </Label>
                        <Button type="submit" disabled={youtubeBusy}>
                          {youtubeBusy ? <RefreshCcw className="animate-spin" /> : <Plus />}
                          {youtubeBusy ? "Importing" : "Import selected"}
                        </Button>
                      </div>
                    </youtubeFetcher.Form>
                  ) : (
                    <p className="text-muted-foreground text-sm">No subscriptions returned for this YouTube account.</p>
                  )}
                </div>
              ) : (
                <div className="flex items-center justify-between gap-3">
                  <p className="text-muted-foreground text-sm">Connect YouTube to import channels you subscribe to.</p>
                  <Button asChild>
                    <Link to="/dashboard/youtube/start">
                      <CirclePlay />
                      Connect
                    </Link>
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Activity</CardTitle>
            <CardDescription>{activityRunning ? "Refreshing every few seconds" : "Recent activity"}</CardDescription>
          </CardHeader>
          <CardContent>
            {activity.length ? (
              <div className="grid gap-3">
                {activity.slice(0, 8).map((item) => (
                  <div key={item.id} className="flex items-center gap-2.5 text-sm">
                    <ActivityIcon status={item.status} />
                    <span className="min-w-0 truncate font-medium">{item.title}</span>
                    {item.detail ? (
                      <span className="text-muted-foreground shrink-0 text-xs">— {item.detail}</span>
                    ) : null}
                    <span className="text-muted-foreground ml-auto shrink-0 text-xs tabular-nums">
                      {item.updatedAt ? formatClockTime(item.updatedAt) : ""}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-muted-foreground text-sm">No activity yet — add a source to start indexing.</p>
            )}
          </CardContent>
        </Card>
      </section>

      {summary ? (
        <section className="grid gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-lg font-semibold">Plan &amp; usage</h2>
            {summary.plan_key ? (
              <Badge variant="secondary">{summary.plan_key}</Badge>
            ) : (
              <Badge variant="outline">no active plan</Badge>
            )}
            {summary.period ? (
              <span className="text-muted-foreground text-sm">renews {formatDate(summary.period.end_at)}</span>
            ) : null}
          </div>
          {summary.entitlements.length ? (
            <Card>
              <CardContent className="grid gap-5 pt-6">
                <TooltipProvider>
                  {summary.entitlements.map((entitlement) => (
                    <div key={entitlement.key} className="grid gap-1.5">
                      <div className="flex items-center justify-between gap-3 text-sm">
                        <span className="flex items-center gap-1 font-medium">
                          {entitlement.label}
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <button type="button" className="text-muted-foreground/70 hover:text-foreground" aria-label={`What is ${entitlement.label}?`}>
                                <Info className="size-3.5" />
                              </button>
                            </TooltipTrigger>
                            <TooltipContent>{entitlement.description}</TooltipContent>
                          </Tooltip>
                        </span>
                        <span className="text-muted-foreground tabular-nums">{entitlementValue(entitlement)}</span>
                      </div>
                      {entitlement.unlimited ? null : <Progress value={(entitlement.percent ?? 0) * 100} />}
                    </div>
                  ))}
                  {summary.ai_spend_usd != null ? (
                    <div className="flex items-center justify-between gap-3 text-sm">
                      <span className="flex items-center gap-1 font-medium">
                        AI usage
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <button type="button" className="text-muted-foreground/70 hover:text-foreground" aria-label="What is AI usage?">
                              <Info className="size-3.5" />
                            </button>
                          </TooltipTrigger>
                          <TooltipContent>Actual cost of Gemini transcript cleanup and Voyage embeddings this period.</TooltipContent>
                        </Tooltip>
                      </span>
                      <span className="text-muted-foreground tabular-nums">{formatUSD(summary.ai_spend_usd)} this period</span>
                    </div>
                  ) : null}
                </TooltipProvider>
              </CardContent>
            </Card>
          ) : (
            <p className="text-muted-foreground text-sm">No active plan period yet.</p>
          )}
        </section>
      ) : null}

      <section className="grid gap-3">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">Recent videos</h2>
          <Link to="/dashboard/library" className="text-muted-foreground text-sm hover:underline">
            View library →
          </Link>
        </div>
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
