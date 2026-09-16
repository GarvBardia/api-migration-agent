"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { getHumanReviewQueue, HumanReviewQueueItem, ApiError } from "@/lib/api";
import ReviewItemCard from "@/components/ReviewItemCard";

const TOAST_DURATION_MS = 6000;

export default function ReviewQueuePage() {
  const [items, setItems] = useState<HumanReviewQueueItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await getHumanReviewQueue();
      setItems(data);
      setError(null);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to load review queue."
      );
    }
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  }, [refresh]);

  useEffect(() => {
    // Clear any pending dismiss timer on unmount so it doesn't fire after
    // the page is gone.
    return () => {
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    };
  }, []);

  // Fix (this session): the confirmation message used to live as local
  // state INSIDE ReviewItemCard, which gets removed from `items` (and
  // therefore unmounted) the instant `refresh()` runs -- so the message
  // never got a chance to paint. It now lives here, in the parent, so it
  // survives the child unmounting. The toast is shown BEFORE refresh() is
  // awaited, so the confirmation is visible independent of when the list
  // actually updates.
  function handleDecided(message: string) {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setToast(message);
    toastTimerRef.current = setTimeout(() => setToast(null), TOAST_DURATION_MS);
    refresh();
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="mb-1 text-xs font-medium uppercase tracking-wide text-foreground-muted">
          Needs a decision
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">
          Human review queue
        </h1>
      </div>

      {toast && (
        <div className="flex items-start gap-2 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2.5 text-sm text-blue-900">
          <span
            aria-hidden
            className="mt-0.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-blue-500"
          />
          {toast}
        </div>
      )}
      {error && (
        <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </p>
      )}
      {items === null && (
        <p className="text-sm text-foreground-muted">Loading…</p>
      )}
      {items !== null && items.length === 0 && (
        <div className="rounded-xl border border-dashed border-border-strong bg-surface-muted/40 px-4 py-10 text-center text-sm text-foreground-muted">
          Nothing pending review.
        </div>
      )}
      <div className="flex flex-col gap-4">
        {items?.map((item) => (
          <ReviewItemCard
            key={item.review.id}
            item={item}
            onDecided={handleDecided}
          />
        ))}
      </div>
    </div>
  );
}
