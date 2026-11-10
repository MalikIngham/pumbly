"use client";

import { useState } from "react";

import { PostDate } from "@/components/PostDate";
import type { Post } from "@/lib/api";

export function PostCard({
  post,
  blur,
  onOpen,
}: {
  post: Post;
  blur: boolean;
  onOpen: (post: Post) => void;
}) {
  const [revealed, setRevealed] = useState(false);
  const hidden = blur && post.is_nsfw && !revealed;
  const cover = post.images[0];
  const imageClass = "aspect-square w-full object-cover";

  return (
    <article className="relative flex flex-col overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900">
      {hidden && (
        <button
          type="button"
          onClick={() => setRevealed(true)}
          className="absolute inset-0 z-10 flex items-center justify-center bg-black/30 p-4 text-sm font-medium text-white focus-visible:outline-2 focus-visible:outline-blue-600"
        >
          Mature post. Click to show
        </button>
      )}
      <div
        inert={hidden}
        className={`flex flex-1 flex-col ${hidden ? "blur-xl select-none" : ""}`}
      >
        {cover ? (
          <button
            type="button"
            onClick={() => onOpen(post)}
            aria-label={
              post.images.length > 1
                ? `View all ${post.images.length} images`
                : "View image"
            }
            className="relative block cursor-zoom-in focus-visible:outline-2 focus-visible:outline-blue-600"
          >
            {/* Tumblr's CDN already serves sized images, and a blog can have thousands */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={cover.thumb_url}
              alt={cover.alt}
              loading="lazy"
              referrerPolicy="no-referrer"
              className={imageClass}
            />
            {post.images.length > 1 && (
              <span className="absolute top-2 right-2 rounded bg-black/70 px-2 py-0.5 text-xs font-medium text-white">
                1/{post.images.length}
              </span>
            )}
          </button>
        ) : (
          post.poster_url && (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={post.poster_url}
              alt=""
              loading="lazy"
              referrerPolicy="no-referrer"
              className={imageClass}
            />
          )
        )}
        <div className="flex flex-1 flex-col gap-2 p-4">
          {post.question !== null && (
            <p className="line-clamp-3 text-sm break-words text-gray-600 dark:text-gray-400">
              <span className="font-medium">
                {post.asker ?? "Anonymous"} asked:
              </span>{" "}
              {post.question}
            </p>
          )}
          {post.summary && (
            <p className="line-clamp-4 text-sm break-words">{post.summary}</p>
          )}
          <p className="mt-auto text-xs text-gray-600 dark:text-gray-400">
            {post.is_nsfw && "Mature · "}
            <span className="capitalize">{post.type}</span> ·{" "}
            <PostDate post={post} />
            {post.note_count !== null &&
              ` · ${post.note_count.toLocaleString()} notes`}
          </p>
        </div>
      </div>
    </article>
  );
}
