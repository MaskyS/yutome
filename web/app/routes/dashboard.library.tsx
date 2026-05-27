import { Link } from "react-router";

import type { Route } from "./+types/dashboard.library";
import { getEnv } from "~/lib/env.server";
import { listChannels, listVideos } from "~/lib/hosted-api.server";
import { isUnauthorized, requireSessionToken, signupRedirect } from "~/lib/session.server";
import { formatDate } from "~/lib/utils";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "~/components/ui/table";

export function meta(_: Route.MetaArgs) {
  return [{ title: "Library · Yutome" }];
}

const VIDEO_PAGE = 12;
const CHANNEL_PAGE = 12;
const MAX_LIMIT = 200;

function clampOffset(raw: string | null): number {
  const parsed = Number.parseInt(raw ?? "0", 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function libraryLink(voffset: number, coffset: number): string {
  const params = new URLSearchParams();
  if (voffset > 0) params.set("voffset", String(voffset));
  if (coffset > 0) params.set("coffset", String(coffset));
  const query = params.toString();
  return query ? `/dashboard/library?${query}` : "/dashboard/library";
}

export async function loader({ request, context }: Route.LoaderArgs) {
  const env = getEnv(context);
  const token = requireSessionToken(request);
  const url = new URL(request.url);
  const voffset = clampOffset(url.searchParams.get("voffset"));
  const coffset = clampOffset(url.searchParams.get("coffset"));
  try {
    // Sequential, not Promise.all: each /account/list passes through the usage
    // gate, which opens a transaction on the hosted API's shared psycopg
    // connection, and concurrent transactions on one connection collide
    // (OutOfOrderTransactionNesting). See docs note on hosted API connection pooling.
    const videos = await listVideos(env, token, {
      order_by: "newest",
      limit: Math.min(VIDEO_PAGE + 1, MAX_LIMIT),
      offset: voffset,
    });
    const channels = await listChannels(env, token, {
      limit: Math.min(CHANNEL_PAGE + 1, MAX_LIMIT),
      offset: coffset,
    });
    return {
      videos: videos.slice(0, VIDEO_PAGE),
      channels: channels.slice(0, CHANNEL_PAGE),
      voffset,
      coffset,
      hasNextVideos: videos.length > VIDEO_PAGE,
      hasNextChannels: channels.length > CHANNEL_PAGE,
    };
  } catch (error) {
    if (isUnauthorized(error)) {
      signupRedirect(env);
    }
    throw error;
  }
}

export default function DashboardLibrary({ loaderData }: Route.ComponentProps) {
  const { videos, channels, voffset, coffset, hasNextVideos, hasNextChannels } = loaderData;

  return (
    <div className="grid gap-8">
      <section className="grid gap-1">
        <h1 className="text-xl font-semibold">Library</h1>
        <p className="text-muted-foreground text-sm">Browse the channels and videos you&apos;ve indexed.</p>
      </section>

      <section className="grid gap-3">
        <h2 className="text-lg font-semibold">Channels</h2>
        {channels.length ? (
          <>
            <div className="grid gap-2 sm:grid-cols-2">
              {channels.map((channel) => (
                <Card key={channel.channel_id}>
                  <CardHeader className="pb-3">
                    <CardTitle className="text-base">
                      <Link
                        to={`/dashboard/channel/${encodeURIComponent(channel.channel_id)}`}
                        className="hover:underline"
                      >
                        {channel.title ?? channel.channel_handle ?? channel.channel_id}
                      </Link>
                    </CardTitle>
                    <CardDescription>
                      {channel.video_count != null ? `${channel.video_count.toLocaleString()} videos` : "—"}
                      {channel.channel_handle ? ` · ${channel.channel_handle}` : ""}
                    </CardDescription>
                  </CardHeader>
                </Card>
              ))}
            </div>
            <Pagination
              prev={coffset > 0 ? libraryLink(voffset, Math.max(0, coffset - CHANNEL_PAGE)) : null}
              next={hasNextChannels ? libraryLink(voffset, coffset + CHANNEL_PAGE) : null}
            />
          </>
        ) : (
          <p className="text-muted-foreground text-sm">No channels yet. Add a source from the Overview tab.</p>
        )}
      </section>

      <section className="grid gap-3">
        <h2 className="text-lg font-semibold">Videos</h2>
        {videos.length ? (
          <>
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
                    {videos.map((video) => (
                      <TableRow key={video.video_id}>
                        <TableCell className="font-medium">
                          <Link
                            to={`/dashboard/video/${encodeURIComponent(video.video_id)}`}
                            className="hover:underline"
                          >
                            {video.title ?? video.video_id}
                          </Link>
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {video.channel_title ?? video.channel_handle ?? video.channel_id ?? "—"}
                        </TableCell>
                        <TableCell className="text-muted-foreground text-right">
                          {video.published_at ? formatDate(video.published_at) : "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
            <Pagination
              prev={voffset > 0 ? libraryLink(Math.max(0, voffset - VIDEO_PAGE), coffset) : null}
              next={hasNextVideos ? libraryLink(voffset + VIDEO_PAGE, coffset) : null}
            />
          </>
        ) : (
          <p className="text-muted-foreground text-sm">Nothing indexed yet.</p>
        )}
      </section>
    </div>
  );
}

function Pagination({ prev, next }: { prev: string | null; next: string | null }) {
  if (!prev && !next) return null;
  return (
    <div className="flex items-center gap-2">
      {prev ? (
        <Button asChild size="sm" variant="outline">
          <Link to={prev}>Previous</Link>
        </Button>
      ) : null}
      {next ? (
        <Button asChild size="sm" variant="outline">
          <Link to={next}>Next</Link>
        </Button>
      ) : null}
    </div>
  );
}
