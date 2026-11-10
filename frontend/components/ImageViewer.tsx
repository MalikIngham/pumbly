"use client";

import {
  type KeyboardEvent,
  type MouseEvent,
  type SyntheticEvent,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { PostDate } from "@/components/PostDate";
import type { Post, PostImage } from "@/lib/api";

function imageWidth({ width, height }: PostImage): string {
  if (!width || !height) return "100%";
  // Fits the track without cropping, and never upscales past the real size
  return `min(100cqw, calc(100cqh * ${width / height}), ${width}px)`;
}

function Slide({
  image,
  near,
  onBackdropClick,
}: {
  image: PostImage;
  near: boolean;
  onBackdropClick: (event: MouseEvent) => void;
}) {
  const [requested, setRequested] = useState(near);
  const [loaded, setLoaded] = useState(false);
  if (near && !requested) setRequested(true);

  const known = image.width && image.height;
  const fit = "absolute inset-0 h-full w-full object-contain";

  return (
    <div
      onClick={onBackdropClick}
      className="flex h-full w-full shrink-0 snap-center items-center justify-center"
    >
      <div
        className="relative"
        style={{
          width: imageWidth(image),
          ...(known
            ? { aspectRatio: `${image.width} / ${image.height}` }
            : { height: "100%" }),
        }}
      >
        {!loaded && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={image.thumb_url}
            alt=""
            referrerPolicy="no-referrer"
            className={fit}
          />
        )}
        {requested && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={image.full_url}
            alt={image.alt}
            referrerPolicy="no-referrer"
            onLoad={() => setLoaded(true)}
            className={fit}
          />
        )}
      </div>
    </div>
  );
}

export function ImageViewer({
  post,
  startIndex,
  onClose,
}: {
  post: Post;
  startIndex: number;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const historyPushed = useRef(false);
  const [index, setIndex] = useState(startIndex);

  const { images } = post;
  const current = images[index];
  const several = images.length > 1;

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    const track = trackRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    if (track) track.scrollLeft = startIndex * track.clientWidth;
  }, [startIndex]);

  // Every way of closing goes through history, so the phone back button
  // closes the viewer instead of leaving the search.
  useEffect(() => {
    if (!historyPushed.current) {
      historyPushed.current = true;
      window.history.pushState(null, "");
    }
    function handlePopState() {
      dialogRef.current?.close();
      onClose();
    }
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [onClose]);

  function requestClose() {
    window.history.back();
  }

  function goTo(target: number) {
    const track = trackRef.current;
    if (!track || target < 0 || target >= images.length) return;
    const reduced = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    track.scrollTo({
      left: target * track.clientWidth,
      behavior: reduced ? "auto" : "smooth",
    });
  }

  function handleScroll() {
    const track = trackRef.current;
    if (!track || track.clientWidth === 0) return;
    setIndex(Math.round(track.scrollLeft / track.clientWidth));
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === "ArrowRight") goTo(index + 1);
    if (event.key === "ArrowLeft") goTo(index - 1);
  }

  function handleCancel(event: SyntheticEvent) {
    event.preventDefault();
    requestClose();
  }

  function handleBackdropClick(event: MouseEvent) {
    if (event.target === event.currentTarget) requestClose();
  }

  const arrow =
    "absolute top-1/2 z-10 -translate-y-1/2 rounded-full bg-black/60 px-4 py-3 text-xl hover:bg-black/80 disabled:invisible";

  return (
    <dialog
      ref={dialogRef}
      aria-label={several ? "Post images" : "Post image"}
      onCancel={handleCancel}
      onKeyDown={handleKeyDown}
      className="fixed inset-0 m-0 h-dvh max-h-none w-full max-w-none bg-black p-0 text-white open:flex open:flex-col"
    >
      <div className="flex shrink-0 items-center justify-between px-4 py-3 text-sm">
        <p aria-live="polite">
          {several ? `Image ${index + 1} of ${images.length}` : ""}
        </p>
        <button
          type="button"
          autoFocus
          onClick={requestClose}
          className="rounded px-3 py-1 hover:bg-white/15"
        >
          Close
        </button>
      </div>

      <div className="relative min-h-0 flex-1">
        <div
          ref={trackRef}
          onScroll={handleScroll}
          style={{ containerType: "size", scrollbarWidth: "none" }}
          className="flex h-full snap-x snap-mandatory overflow-x-auto overflow-y-hidden"
        >
          {images.map((image, i) => (
            <Slide
              key={image.full_url}
              image={image}
              near={Math.abs(i - index) <= 1}
              onBackdropClick={handleBackdropClick}
            />
          ))}
        </div>
        {several && (
          <>
            <button
              type="button"
              aria-label="Previous image"
              disabled={index === 0}
              onClick={() => goTo(index - 1)}
              className={`${arrow} left-2`}
            >
              &larr;
            </button>
            <button
              type="button"
              aria-label="Next image"
              disabled={index === images.length - 1}
              onClick={() => goTo(index + 1)}
              className={`${arrow} right-2`}
            >
              &rarr;
            </button>
          </>
        )}
      </div>

      <div className="max-h-[30dvh] shrink-0 overflow-y-auto px-4 py-3 text-sm">
        {post.question !== null && (
          <p className="mb-2 break-words whitespace-pre-line text-gray-300">
            <span className="font-medium">
              {post.asker ?? "Anonymous"} asked:
            </span>{" "}
            {post.question}
          </p>
        )}
        {post.summary && (
          <p className="mb-2 break-words whitespace-pre-line">{post.summary}</p>
        )}
        <p className="flex gap-4 text-gray-300">
          {current && (
            <a
              href={current.full_url}
              target="_blank"
              rel="noopener noreferrer"
              className="underline hover:text-white"
            >
              Open original
            </a>
          )}
          <PostDate post={post} />
        </p>
      </div>
    </dialog>
  );
}
