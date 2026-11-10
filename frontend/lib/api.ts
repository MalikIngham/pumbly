const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export const loginUrl = `${API_URL}/api/auth/login`;
export const POSTS_PER_PAGE = 100;

export function authReturnUrl(search: string): string {
  return `${API_URL}/api/auth/callback${search}`;
}

export type ScanState =
  | "queued"
  | "running"
  | "paused"
  | "complete"
  | "failed";

export interface ScanStatus {
  id: string;
  blog: string;
  status: ScanState;
  scanned: number;
  total: number | null;
  originals: number;
  new_originals: number;
  error: string | null;
}

export interface PostImage {
  full_url: string;
  thumb_url: string;
  width: number | null;
  height: number | null;
  alt: string;
  from_ask: boolean;
}

export interface Post {
  id: string;
  url: string;
  type: string;
  timestamp: number | null;
  note_count: number | null;
  summary: string;
  asker: string | null;
  question: string | null;
  images: PostImage[];
  poster_url: string | null;
  video_urls: string[];
  external_links: string[];
  is_nsfw: boolean;
}

interface PostPage {
  posts: Post[];
  next_after: number;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, init);
  } catch (error) {
    if (init?.signal?.aborted) throw error;
    throw new ApiError("Could not reach the Pumbly server.", 0);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const message =
      typeof body?.detail === "string"
        ? body.detail
        : "Something went wrong. Please try again.";
    throw new ApiError(message, response.status);
  }
  return response.json();
}

export interface AuthStatus {
  configured: boolean;
  connected: boolean;
}

export function getAuthStatus(): Promise<AuthStatus> {
  return request("/api/auth/status");
}

export function startScan(blog: string): Promise<ScanStatus> {
  return request("/api/scans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ blog }),
  });
}

export function getScan(id: string, signal: AbortSignal): Promise<ScanStatus> {
  return request(`/api/scans/${id}`, { signal });
}

export function getPosts(
  id: string,
  after: number,
  signal: AbortSignal,
): Promise<PostPage> {
  return request(`/api/scans/${id}/posts?after=${after}&limit=${POSTS_PER_PAGE}`, {
    signal,
  });
}

export function stopScan(id: string): Promise<ScanStatus> {
  return request(`/api/scans/${id}/stop`, { method: "POST" });
}

export async function requestDownload(id: string): Promise<string> {
  const { url } = await request<{ url: string }>(`/api/scans/${id}/downloads`, {
    method: "POST",
  });
  return `${API_URL}${url}`;
}
