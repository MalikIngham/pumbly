import type { Post } from "@/lib/api";

function formatDate(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function PostDate({ post }: { post: Post }) {
  return (
    <a
      href={post.url}
      target="_blank"
      rel="noopener noreferrer"
      className="underline hover:no-underline"
    >
      {post.timestamp !== null ? formatDate(post.timestamp) : "View post"}
    </a>
  );
}
