"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  fetchChunks,
  ingestRepo,
  type RepoChunksResponse,
  type RepoFilesResponse,
} from "@/lib/api";

import { EmbedAndSearch } from "./EmbedAndSearch";

export function IngestForm() {
  const [url, setUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RepoFilesResponse | null>(null);
  const [chunkSummary, setChunkSummary] = useState<RepoChunksResponse | null>(
    null,
  );

  const handleSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      setLoading(true);
      setError(null);
      setChunkSummary(null);
      try {
        const data = await ingestRepo(url);
        setResult(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Unknown error");
        setResult(null);
      } finally {
        setLoading(false);
      }
    },
    [url],
  );

  // After an ingest, pull the chunk summary in a second request. (The /ingest
  // response stays additive-only — chunks aren't embedded in it.) Limit=0
  // would skip rows entirely but we still need `summary`, so we ask for the
  // smallest page that returns the totals we need.
  useEffect(() => {
    if (!result) return;
    let cancelled = false;
    fetchChunks(result.repo.id, { limit: 1, offset: 0 })
      .then((data) => {
        if (!cancelled) setChunkSummary(data);
      })
      .catch(() => {
        // Chunking can fail without failing ingest; the UI just hides the panel.
        if (!cancelled) setChunkSummary(null);
      });
    return () => {
      cancelled = true;
    };
  }, [result]);

  return (
    <div className="space-y-6">
      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://github.com/owner/name"
          required
          disabled={loading}
          className="flex-1 rounded border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-gray-500 focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={loading || !url}
          className="rounded bg-gray-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-gray-700 disabled:opacity-50"
        >
          {loading ? "Ingesting…" : "Ingest"}
        </button>
      </form>

      {error && (
        <div className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          {error}
        </div>
      )}

      {result && <ResultTable result={result} />}
      {chunkSummary && chunkSummary.total > 0 && (
        <ChunkSummaryPanel data={chunkSummary} />
      )}
      {result && (
        <EmbedAndSearch key={result.repo.id} repoId={result.repo.id} />
      )}
    </div>
  );
}

function ResultTable({ result }: { result: RepoFilesResponse }) {
  return (
    <section className="space-y-3">
      <header className="flex items-baseline justify-between">
        <h2 className="text-lg font-medium">
          {result.repo.owner}/{result.repo.name}
        </h2>
        <span className="text-xs text-gray-500">
          {result.file_count} file{result.file_count === 1 ? "" : "s"} · branch{" "}
          <span className="font-mono">{result.repo.default_branch}</span>
        </span>
      </header>
      <div className="overflow-hidden rounded border border-gray-200">
        <table className="min-w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-3 py-2">Path</th>
              <th className="px-3 py-2">Language</th>
              <th className="px-3 py-2 text-right">Size</th>
              <th className="px-3 py-2 text-right">Chunks</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200">
            {result.files.map((file) => (
              <tr key={file.path}>
                <td className="px-3 py-1.5 font-mono text-xs">{file.path}</td>
                <td className="px-3 py-1.5 text-gray-600">
                  {file.language ?? "—"}
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums text-gray-500">
                  {formatBytes(file.size_bytes)}
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums text-gray-500">
                  {file.chunk_count > 0 ? file.chunk_count : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ChunkSummaryPanel({ data }: { data: RepoChunksResponse }) {
  const byType = Object.entries(data.summary.by_type).sort(
    ([, a], [, b]) => b - a,
  );
  const byLanguage = Object.entries(data.summary.by_language).sort(
    ([, a], [, b]) => b - a,
  );

  return (
    <section className="space-y-3">
      <header className="flex items-baseline justify-between">
        <h2 className="text-lg font-medium">Chunks</h2>
        <span className="text-xs text-gray-500">
          {data.total} total
        </span>
      </header>
      <div className="grid grid-cols-2 gap-4">
        <div className="rounded border border-gray-200 p-3">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
            By type
          </h3>
          <ul className="space-y-1 text-sm">
            {byType.map(([type, count]) => (
              <li key={type} className="flex justify-between">
                <span className="font-mono text-xs">{type}</span>
                <span className="tabular-nums text-gray-600">{count}</span>
              </li>
            ))}
          </ul>
        </div>
        <div className="rounded border border-gray-200 p-3">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
            By language
          </h3>
          <ul className="space-y-1 text-sm">
            {byLanguage.map(([lang, count]) => (
              <li key={lang} className="flex justify-between">
                <span>{lang}</span>
                <span className="tabular-nums text-gray-600">{count}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
