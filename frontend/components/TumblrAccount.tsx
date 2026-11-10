"use client";

import { useEffect, useState } from "react";

import {
  type AuthStatus,
  authReturnUrl,
  getAuthStatus,
  loginUrl,
} from "@/lib/api";

const FAILURE_MESSAGES: Record<string, string> = {
  failed: "Could not connect your Tumblr account. Please try again.",
  mismatch:
    "Tumblr rejected the connection. Add http://localhost:8000/api/auth/callback to the OAuth2 redirect URLs of the app matching your consumer key, then connect again.",
};

export function TumblrAccount() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    async function load() {
      // Tumblr returns to whichever address the app registered, which can be
      // this page, so its answer is handed on to the backend
      const incoming = new URLSearchParams(window.location.search);
      if (incoming.has("code") || incoming.has("error")) {
        window.location.replace(authReturnUrl(window.location.search));
        return;
      }

      const current = await getAuthStatus().catch(() => null);
      if (!active) return;

      // Only after the await: development runs this effect twice, and the
      // abandoned first run must not clear the URL before this one reads it
      const params = new URLSearchParams(window.location.search);
      if (params.has("tumblr")) {
        window.history.replaceState(null, "", window.location.pathname);
      }
      setStatus(current);
      setFailure(FAILURE_MESSAGES[params.get("tumblr") ?? ""] ?? null);
    }

    load();
    return () => {
      active = false;
    };
  }, []);

  if (status === null) return null;

  return (
    <div className="text-center text-sm text-gray-600 dark:text-gray-400">
      {failure && (
        <p role="alert" className="mb-1 text-red-700 dark:text-red-400">
          {failure}
        </p>
      )}
      {!status.configured && (
        <p>Add TUMBLR_CONSUMER_SECRET to backend/.env to see login-only blogs.</p>
      )}
      {status.configured && status.connected && (
        <p>Tumblr account connected. Login-only blogs are included.</p>
      )}
      {status.configured && !status.connected && (
        <p>
          <a href={loginUrl} className="underline hover:no-underline">
            Connect your Tumblr account
          </a>{" "}
          to see login-only blogs.
        </p>
      )}
    </div>
  );
}
