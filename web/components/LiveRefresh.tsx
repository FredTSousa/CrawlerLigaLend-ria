"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

// Re-fetches the server component periodically while a match is live.
export default function LiveRefresh({
  active,
  intervalMs = 15000,
}: {
  active: boolean;
  intervalMs?: number;
}) {
  const router = useRouter();
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => router.refresh(), intervalMs);
    return () => clearInterval(t);
  }, [active, intervalMs, router]);
  return null;
}
