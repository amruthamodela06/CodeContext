"use client";

import {
  useCallback,
  useEffect,
  useState,
  type FormEvent,
} from "react";

import {
  fetchEmbeddingStatus,
  search,
  triggerEmbed,
  type EmbeddingStatus,
  type SearchResultItem,
} from "@/lib/api";

export function EmbedAndSearch({ repoId }: { repoId: number }) {
  const [status, setStatus] = useState<EmbeddingStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Load status on mount. The parent remounts this component per repo (via a
  // `key`), so there's no need to reset state synchronously here.
  useEffect(() => {
    let cancelled = false;
    fetchEmbeddingStatus(repoId)
      .then((s) => {
        if (!cancelled) setStatus(s);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [repoId]);

  // Poll while embedding is in progress.
  const phase = status?.embedding_status;
  useEffect(() => {
    if (phase !== "in_progress") return;
    const id = setInterval(() => {
      fetchEmbeddingStatus(repoId)
        .then(setStatus)
        .catch(() => {});
    }, 2000);
    return () => clearInterval(id);
  }, [repoId, phase]);

  const onEmbed = useCallback(async () => {
    setError(null);
    try {
      await triggerEmbed(repoId);
      setStatus(await fetchEmbeddingStatus(repoId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start embedding");
    }
  }, [repoId]);

  const st = status?.embedding_status ?? "pending";

  return (
    <section className="space-y-3">
      <header className="flex items-baseline justify-between">
        <h2 className="text-lg font-medium">Embeddings &amp; search</h2>
        <StatusPill status={st} />
      </header>

      {error && (
        <div className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          {error}
        </div>
      )}

      {(st === "pending" || st === "failed") && (
        <button
          onClick={onEmbed}
          className="rounded bg-gray-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-gray-700"
        >
          {st === "failed" ? "Retry embedding" : "Generate embeddings"}
        </button>
      )}
      {(st === "pending" || st === "failed") && (
        <p className="text-xs text-gray-500">
          Runs locally on CPU (bge-small). Large repos can take several minutes.
        </p>
      )}

      {st === "in_progress" && status && (
        <ProgressBar
          progress={status.embedding_progress}
          embedded={status.chunks_embedded}
          total={status.chunks_total}
        />
      )}

      {st === "done" && <SearchPanel repoId={repoId} />}
    </section>
  );
}

function StatusPill({ status }: { status: string }) {
  const styles: Record<string, string> = {
    pending: "bg-gray-100 text-gray-600",
    in_progress: "bg-amber-100 text-amber-800",
    done: "bg-green-100 text-green-800",
    failed: "bg-red-100 text-red-800",
  };
  const label: Record<string, string> = {
    pending: "not embedded",
    in_progress: "embedding…",
    done: "ready",
    failed: "failed",
  };
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-xs font-medium ${styles[status] ?? styles.pending}`}
    >
      {label[status] ?? status}
    </span>
  );
}

function ProgressBar({
  progress,
  embedded,
  total,
}: {
  progress: number;
  embedded: number;
  total: number;
}) {
  const pct = Math.round(progress * 100);
  return (
    <div className="space-y-1">
      <div className="h-2 w-full overflow-hidden rounded bg-gray-100">
        <div
          className="h-full bg-amber-500 transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
      <p className="text-xs text-gray-500">
        {embedded}/{total} chunks · {pct}%
      </p>
    </div>
  );
}

function SearchPanel({ repoId }: { repoId: number }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResultItem[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      setSearching(true);
      setError(null);
      try {
        const resp = await search(repoId, query, 5);
        setResults(resp.results);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Search failed");
        setResults(null);
      } finally {
        setSearching(false);
      }
    },
    [repoId, query],
  );

  return (
    <div className="space-y-3">
      <form onSubmit={onSubmit} className="flex gap-2">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search the codebase by meaning…"
          disabled={searching}
          className="flex-1 rounded border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-gray-500 focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={searching || !query}
          className="rounded bg-gray-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-gray-700 disabled:opacity-50"
        >
          {searching ? "Searching…" : "Search"}
        </button>
      </form>

      {error && (
        <div className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          {error}
        </div>
      )}

      {results && results.length === 0 && (
        <p className="text-sm text-gray-500">No results.</p>
      )}

      {results && results.length > 0 && (
        <ul className="space-y-2">
          {results.map((r) => (
            <li
              key={r.chunk_id}
              className="rounded border border-gray-200 p-3"
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-mono text-xs text-gray-700">
                  {r.file_path}:{r.start_line}-{r.end_line}
                </span>
                <span className="shrink-0 text-xs tabular-nums text-gray-500">
                  {r.similarity.toFixed(3)}
                </span>
              </div>
              <div className="mt-0.5 text-xs text-gray-500">
                {r.chunk_type}
                {r.name ? ` · ${r.name}` : ""} · {r.language}
              </div>
              <pre className="mt-2 overflow-x-auto rounded bg-gray-50 p-2 font-mono text-xs leading-relaxed text-gray-800">
                {previewLines(r.content)}
              </pre>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function previewLines(content: string, lines = 5): string {
  const split = content.split("\n");
  const head = split.slice(0, lines).join("\n");
  return split.length > lines ? `${head}\n…` : head;
}
