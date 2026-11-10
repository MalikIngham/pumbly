"use client";

import { type FormEvent, useCallback, useEffect, useState } from "react";

import { ImageViewer } from "@/components/ImageViewer";
import { PostCard } from "@/components/PostCard";
import {
  ApiError,
  getPosts,
  getScan,
  type Post,
  POSTS_PER_PAGE,
  requestDownload,
  type ScanStatus,
  startScan,
  stopScan,
} from "@/lib/api";

const POLL_MS = 1500;
const MAX_POLL_FAILURES = 5;
const PAGE_SIZE = 60;
const SECONDARY_BUTTON =
  "rounded border border-gray-300 px-6 py-2 text-sm font-medium transition hover:border-blue-500 disabled:opacity-50 dark:border-gray-700";

function isActive(scan: ScanStatus): boolean {
  return scan.status === "queued" || scan.status === "running";
}

const PROGRESS_VERBS: Partial<Record<ScanStatus["status"], string>> = {
  running: "Checked",
  paused: "Paused at",
  failed: "Stopped at",
};

function progressLabel(scan: ScanStatus): string {
  if (scan.status === "queued") return "Waiting for a free slot...";
  const of = scan.total === null ? "" : ` of ${scan.total.toLocaleString()}`;
  const verb = PROGRESS_VERBS[scan.status] ?? "Scanned";
  return `${verb} ${scan.scanned.toLocaleString()}${of} posts`;
}

// New posts arrive out of order during a rescan, so newest first is enforced here
function mergeNewestFirst(current: Post[], fresh: Post[]): Post[] {
  const known = new Set(current.map((post) => post.id));
  return [...current, ...fresh.filter((post) => !known.has(post.id))].sort(
    (a, b) => (b.timestamp ?? 0) - (a.timestamp ?? 0),
  );
}

export function BlogSearch() {
  const [blog, setBlog] = useState("");
  const [scan, setScan] = useState<ScanStatus | null>(null);
  const [posts, setPosts] = useState<Post[]>([]);
  const [visible, setVisible] = useState(PAGE_SIZE);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [viewing, setViewing] = useState<Post | null>(null);
  const closeViewer = useCallback(() => setViewing(null), []);
  const [preparingDownload, setPreparingDownload] = useState(false);
  const [blurMature, setBlurMature] = useState(false);

  const [runCount, setRunCount] = useState(0);
  const [stopping, setStopping] = useState(false);

  const scanId = scan?.id ?? null;

  useEffect(() => {
    if (scanId === null) return;

    const controller = new AbortController();
    const { signal } = controller;
    let timer: ReturnType<typeof setTimeout>;
    let after = 0;
    let failures = 0;

    async function poll(id: string) {
      try {
        const status = await getScan(id, signal);
        const fresh: Post[] = [];
        let position = after;
        for (;;) {
          const page = await getPosts(id, position, signal);
          fresh.push(...page.posts);
          position = page.next_after;
          if (page.posts.length < POSTS_PER_PAGE) break;
        }
        if (signal.aborted) return;

        failures = 0;
        after = position;
        setScan(status);
        if (fresh.length > 0) {
          setPosts((current) => mergeNewestFirst(current, fresh));
        }
        if (isActive(status)) timer = setTimeout(() => poll(id), POLL_MS);
      } catch (caught) {
        if (signal.aborted) return;
        failures += 1;
        if (
          (caught instanceof ApiError && caught.status === 404) ||
          failures >= MAX_POLL_FAILURES
        ) {
          setError(
            caught instanceof Error ? caught.message : "Lost track of the scan.",
          );
          setScan((current) =>
            current && { ...current, status: "failed", error: null },
          );
          return;
        }
        timer = setTimeout(() => poll(id), POLL_MS);
      }
    }

    poll(scanId);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [scanId, runCount]);

  async function runScan(name: string) {
    setSubmitting(true);
    setError(null);
    try {
      const started = await startScan(name);
      setPosts([]);
      setVisible(PAGE_SIZE);
      setScan(started);
      setRunCount((count) => count + 1);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Something went wrong.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (blog.trim() === "" || submitting) return;
    runScan(blog);
  }

  async function handleStop() {
    if (!scanId || stopping) return;

    setStopping(true);
    try {
      setScan(await stopScan(scanId));
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Something went wrong.",
      );
    } finally {
      setStopping(false);
    }
  }

  async function handleDownload() {
    if (!scanId || preparingDownload) return;

    setPreparingDownload(true);
    setError(null);
    try {
      // The zip is sent as an attachment, so this does not leave the page
      const link = document.createElement("a");
      link.href = await requestDownload(scanId);
      link.click();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Something went wrong.",
      );
    } finally {
      setPreparingDownload(false);
    }
  }

  const matureCount = posts.filter((post) => post.is_nsfw).length;

  const percent =
    scan?.total && scan.total > 0
      ? Math.min(100, (scan.scanned / scan.total) * 100)
      : undefined;

  return (
    <div className="flex w-full flex-col items-center gap-8">
      <form onSubmit={handleSubmit} className="flex w-full max-w-md gap-2">
        <label htmlFor="blog-name" className="sr-only">
          Tumblr blog name
        </label>
        <input
          id="blog-name"
          type="search"
          value={blog}
          onChange={(event) => setBlog(event.target.value)}
          placeholder="Input blog name"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          className="min-w-0 flex-1 rounded border border-gray-300 bg-white px-3 py-2 text-gray-900 focus:border-blue-600 focus:outline-none dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
        />
        <button
          type="submit"
          disabled={submitting || blog.trim() === ""}
          className="rounded bg-blue-600 px-6 py-2 text-sm font-medium text-white transition hover:bg-blue-700 disabled:opacity-50"
        >
          {submitting ? "Starting..." : "Search"}
        </button>
      </form>

      {error && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {error}
        </p>
      )}

      {scan && (
        <section className="flex w-full flex-col items-center gap-6">
          <div role="status" className="w-full max-w-md text-center text-sm">
            {isActive(scan) && (
              <progress
                aria-label="Scan progress"
                value={percent}
                max={100}
                className="mb-2 h-2 w-full"
              />
            )}
            {(scan.status !== "failed" || scan.scanned > 0) && (
              <p>
                <span className="font-medium">{scan.blog}</span>:{" "}
                {progressLabel(scan)}, {scan.originals.toLocaleString()}{" "}
                original
                {scan.new_originals > 0 &&
                  ` (${scan.new_originals.toLocaleString()} new)`}
              </p>
            )}
            {scan.status === "complete" && scan.originals === 0 && (
              <p className="mt-2">This blog has no original posts.</p>
            )}
            {scan.error && (
              <p className="mt-2 text-red-700 dark:text-red-400">
                {scan.error}
              </p>
            )}
          </div>

          <div className="flex flex-wrap justify-center gap-2">
            {isActive(scan) && (
              <button
                type="button"
                onClick={handleStop}
                disabled={stopping}
                className={SECONDARY_BUTTON}
              >
                {stopping ? "Stopping..." : "Stop"}
              </button>
            )}
            {(scan.status === "paused" ||
              (scan.status === "failed" && scan.scanned > 0)) && (
              <button
                type="button"
                onClick={() => runScan(scan.blog)}
                disabled={submitting}
                className={SECONDARY_BUTTON}
              >
                Resume
              </button>
            )}
            {scan.originals > 0 && (
              <button
                type="button"
                onClick={handleDownload}
                disabled={preparingDownload}
                className="rounded bg-blue-600 px-6 py-2 text-sm font-medium text-white transition hover:bg-blue-700 disabled:opacity-50"
              >
                {preparingDownload
                  ? "Starting..."
                  : scan.status === "complete"
                    ? `Download all ${scan.originals.toLocaleString()} posts`
                    : `Download ${scan.originals.toLocaleString()} posts so far`}
              </button>
            )}
          </div>

          {posts.length > 0 && (
            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={blurMature}
                onChange={(event) => setBlurMature(event.target.checked)}
                className="h-4 w-4"
              />
              Blur mature posts
              {matureCount > 0 && ` (${matureCount.toLocaleString()})`}
            </label>
          )}

          {posts.length > 0 && (
            <div className="grid w-full grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {posts.slice(0, visible).map((post) => (
                // Remounting on toggle re-blurs any posts revealed earlier
                <PostCard
                  key={`${post.id}-${blurMature}`}
                  post={post}
                  blur={blurMature}
                  onOpen={setViewing}
                />
              ))}
            </div>
          )}

          {posts.length > visible && (
            <button
              type="button"
              onClick={() => setVisible((count) => count + PAGE_SIZE)}
              className="rounded border border-gray-300 px-6 py-2 text-sm font-medium transition hover:border-blue-500 dark:border-gray-700"
            >
              Show more ({(posts.length - visible).toLocaleString()} left)
            </button>
          )}
        </section>
      )}

      {viewing && (
        <ImageViewer post={viewing} startIndex={0} onClose={closeViewer} />
      )}
    </div>
  );
}
