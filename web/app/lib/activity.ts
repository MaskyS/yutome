// Turns the raw hosted job list (/account/source-jobs) into the dashboard
// "Activity" feed: one plain-language row per thing the user added, with
// per-source progress rolled up. Engine internals (job_type, raw JobStatus,
// vector/bm25 rebuilds) never reach the UI.
//
// Pure and framework-free so it can run in the loader and be unit-tested. It
// imports only the `SourceJob` *type* (erased at build), not server runtime.
import type { SourceJob } from "./hosted-api.server";

export type ActivityKind = "video" | "channel" | "playlist" | "subscriptions" | "maintenance";
export type ActivityStatus = "working" | "done" | "failed";

export interface ActivityItem {
  /** Stable key: the source id, or "maintenance" for the rollup row. */
  id: string;
  kind: ActivityKind;
  status: ActivityStatus;
  /** Composed headline, e.g. `Adding @Numberphile` or `Indexed "Monty Hall Problem"`. */
  title: string;
  /** Optional muted secondary, e.g. `12 of 24 videos` or an error message. */
  detail?: string;
  /** Most recent ISO timestamp on the underlying jobs; drives ordering + relative time. */
  updatedAt: string | null;
}

// Internal jobs the user shouldn't see as discrete rows — collapsed into one
// "Updating your library" line, and only while one is actually running.
const MAINTENANCE_JOB_TYPES = new Set([
  "build_vector_index",
  "rebuild_bm25",
  "backfill_embeddings",
  "reindex_workspace",
]);
const FAILED_STATUSES = new Set(["failed", "denied", "cancelled"]);

function bucket(status: string): ActivityStatus {
  if (status === "succeeded") return "done";
  if (FAILED_STATUSES.has(status)) return "failed";
  return "working";
}

function isMaintenance(job: SourceJob): boolean {
  return MAINTENANCE_JOB_TYPES.has(job.job_type) || job.source_id == null;
}

function metaString(job: SourceJob, key: string): string | undefined {
  const value = job.metadata?.[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

/** Newest of finished/started/created across a group, for ordering. */
function latest(jobs: SourceJob[]): string | null {
  let best: string | null = null;
  for (const job of jobs) {
    const at = job.finished_at ?? job.started_at ?? job.created_at;
    if (at && (best === null || at > best)) best = at;
  }
  return best;
}

function kindFromSourceType(sourceType: string | null | undefined): ActivityKind {
  switch (sourceType) {
    case "video":
      return "video";
    case "playlist":
      return "playlist";
    case "subscriptions":
    case "subscription_collection":
      return "subscriptions";
    case "channel":
    default:
      // Default groups (multiple index_video jobs under one discovered source)
      // read as channels — the common case for a handle/channel add.
      return "channel";
  }
}

function videoVerb(status: ActivityStatus): string {
  return status === "done" ? "Indexed" : status === "failed" ? "Couldn't index" : "Indexing";
}

function collectionVerb(kind: ActivityKind, status: ActivityStatus): string {
  if (kind === "subscriptions") {
    return status === "done" ? "Imported" : status === "failed" ? "Couldn't import" : "Importing";
  }
  return status === "done" ? "Added" : status === "failed" ? "Couldn't add" : "Adding";
}

function quoted(name: string): string {
  return `"${name}"`;
}

function videoName(job: SourceJob): string {
  const title =
    typeof job.video_title === "string" && job.video_title.trim() ? job.video_title : metaString(job, "title");
  if (title) return quoted(title);
  const videoId = metaString(job, "youtube_video_id");
  return videoId ? quoted(videoId) : "a video";
}

function sourceLabel(kind: ActivityKind, group: SourceJob[]): string {
  if (kind === "subscriptions") return "your subscriptions";
  if (kind === "channel") {
    for (const job of group) {
      const handle = metaString(job, "channel_handle");
      if (handle) return handle.startsWith("@") ? handle : `@${handle}`;
    }
  }
  const named = group.find((job) => job.source_display_name)?.source_display_name;
  if (named) return named;
  if (kind === "channel") {
    for (const job of group) {
      const channelTitle = metaString(job, "channel_title");
      if (channelTitle) return channelTitle;
    }
  }
  return kind === "playlist" ? "a playlist" : "a source";
}

function errorText(group: SourceJob[]): string | undefined {
  const failed = group.find((job) => bucket(job.status) === "failed" && job.error_message);
  return failed?.error_message ?? undefined;
}

function singleVideoItem(sourceId: string, group: SourceJob[]): ActivityItem {
  // A manual single-video source: one index_video job carries the whole story.
  const job = group.find((j) => j.job_type === "index_video") ?? group[0];
  const status = bucket(job.status);
  return {
    id: sourceId,
    kind: "video",
    status,
    title: `${videoVerb(status)} ${videoName(job)}`,
    detail: status === "failed" ? errorText(group) : undefined,
    updatedAt: latest(group),
  };
}

function collectionItem(sourceId: string, kind: ActivityKind, group: SourceJob[]): ActivityItem {
  const videoJobs = group.filter((job) => job.job_type === "index_video");
  const total = videoJobs.length;
  const done = videoJobs.filter((job) => job.status === "succeeded").length;
  const active = group.some((job) => bucket(job.status) === "working");
  const allFailed = !active && group.every((job) => bucket(job.status) === "failed");

  const status: ActivityStatus = active || (total > 0 && done < total) ? "working" : allFailed ? "failed" : "done";

  let detail: string | undefined;
  if (status === "failed") {
    detail = errorText(group);
  } else if (total > 0) {
    detail = status === "done" ? `${total} videos` : `${done} of ${total} videos`;
  } else if (status === "working") {
    detail = "finding videos";
  }

  return {
    id: sourceId,
    kind,
    status,
    title: `${collectionVerb(kind, status)} ${sourceLabel(kind, group)}`,
    detail,
    updatedAt: latest(group),
  };
}

/**
 * Group jobs into user-facing activity rows, newest first. Maintenance jobs are
 * collapsed to a single "Updating your library" row shown only while running.
 */
export function toActivity(jobs: SourceJob[]): ActivityItem[] {
  const items: ActivityItem[] = [];

  const maintenanceRunning = jobs.filter((job) => isMaintenance(job) && bucket(job.status) === "working");
  if (maintenanceRunning.length) {
    items.push({
      id: "maintenance",
      kind: "maintenance",
      status: "working",
      title: "Updating your library",
      updatedAt: latest(maintenanceRunning),
    });
  }

  const bySource = new Map<string, SourceJob[]>();
  for (const job of jobs) {
    if (isMaintenance(job) || job.source_id == null) continue;
    const group = bySource.get(job.source_id) ?? [];
    group.push(job);
    bySource.set(job.source_id, group);
  }

  for (const [sourceId, group] of bySource) {
    const kind = kindFromSourceType(group.find((job) => job.source_type)?.source_type);
    items.push(kind === "video" ? singleVideoItem(sourceId, group) : collectionItem(sourceId, kind, group));
  }

  items.sort((a, b) => (b.updatedAt ?? "").localeCompare(a.updatedAt ?? ""));
  return items;
}

/** True while any row is still in progress — used to gate loader revalidation. */
export function hasActiveActivity(items: ActivityItem[]): boolean {
  return items.some((item) => item.status === "working");
}
