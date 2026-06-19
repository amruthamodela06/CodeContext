"use client";

import Editor from "@monaco-editor/react";
import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";

import {
  streamQuery,
  type CitedChunkItem,
  type ResolvedCitation,
} from "@/lib/api";

type Phase = "idle" | "streaming" | "done" | "error";

// Mirrors the backend parser's shape-only token (ADR 0010).
const TOKEN_RE = /\[chunk:(none|[A-Za-z0-9_-]{1,16})\]/g;

const MONACO_LANG: Record<string, string> = {
  Python: "python",
  TypeScript: "typescript",
  JavaScript: "javascript",
  Go: "go",
  Rust: "rust",
};

export function QueryPanel({ repoId }: { repoId: number }) {
  const [question, setQuestion] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [answer, setAnswer] = useState("");
  const [sources, setSources] = useState<CitedChunkItem[]>([]);
  const [citations, setCitations] = useState<Map<string, ResolvedCitation> | null>(
    null,
  );
  const [warnings, setWarnings] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const sourcesById = useMemo(() => {
    const map = new Map<string, CitedChunkItem>();
    for (const s of sources) map.set(s.display_id, s);
    return map;
  }, [sources]);

  const onSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      setPhase("streaming");
      setAnswer("");
      setSources([]);
      setCitations(null);
      setWarnings([]);
      setError(null);
      setExpanded(null);

      try {
        await streamQuery(repoId, question, 5, {
          signal: controller.signal,
          onSources: setSources,
          onToken: (text) => setAnswer((prev) => prev + text),
          onCitations: (cites, warns) => {
            setCitations(new Map(cites.map((c) => [c.display_id, c])));
            setWarnings(warns);
          },
          onError: (message) => {
            setError(message);
            setPhase("error");
          },
          onDone: () => setPhase("done"),
        });
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof Error ? err.message : "Query failed");
        setPhase("error");
      }
    },
    [repoId, question],
  );

  const toggleExpand = useCallback((displayId: string) => {
    setExpanded((cur) => (cur === displayId ? null : displayId));
  }, []);

  const rendered = useMemo(
    () => renderAnswer(answer, citations, toggleExpand),
    [answer, citations, toggleExpand],
  );

  const expandedChunk = expanded ? sourcesById.get(expanded) : undefined;
  const expandedCite = expanded ? citations?.get(expanded) : undefined;

  return (
    <div className="space-y-4">
      <form onSubmit={onSubmit} className="flex gap-2">
        <input
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask a question about this repo…"
          disabled={phase === "streaming"}
          className="flex-1 rounded border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-gray-500 focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={phase === "streaming" || !question}
          className="rounded bg-gray-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-gray-700 disabled:opacity-50"
        >
          {phase === "streaming" ? "Answering…" : "Ask"}
        </button>
      </form>

      {error && (
        <div className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          {error}
        </div>
      )}

      {(phase !== "idle" || answer) && (
        <article className="rounded border border-gray-200 p-4">
          <div className="whitespace-pre-wrap text-sm leading-relaxed text-gray-800">
            {rendered}
            {phase === "streaming" && <span className="animate-pulse">▌</span>}
          </div>
          {warnings.length > 0 && (
            <p className="mt-3 text-xs text-amber-700">
              Note: {warnings.join(", ")}
            </p>
          )}
        </article>
      )}

      {expandedChunk && (
        <ChunkView chunk={expandedChunk} permalink={expandedCite?.permalink ?? null} />
      )}

      {sources.length > 0 && (
        <SourcesPanel
          sources={sources}
          citations={citations}
          expanded={expanded}
          onToggle={toggleExpand}
        />
      )}
    </div>
  );
}

function renderAnswer(
  answer: string,
  citations: Map<string, ResolvedCitation> | null,
  onCite: (displayId: string) => void,
): ReactNode[] {
  // Until citations resolve (mid-stream), show raw text — tokens linkify once
  // the final citations event arrives.
  if (!citations) return [answer];
  const nodes: ReactNode[] = [];
  let last = 0;
  let key = 0;
  TOKEN_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = TOKEN_RE.exec(answer)) !== null) {
    if (m.index > last) nodes.push(answer.slice(last, m.index));
    const id = m[1];
    nodes.push(
      <CitationRef
        key={`c-${key++}`}
        displayId={id}
        cite={citations.get(id)}
        onClick={onCite}
      />,
    );
    last = m.index + m[0].length;
  }
  if (last < answer.length) nodes.push(answer.slice(last));
  return nodes;
}

function CitationRef({
  displayId,
  cite,
  onClick,
}: {
  displayId: string;
  cite: ResolvedCitation | undefined;
  onClick: (displayId: string) => void;
}) {
  const status = displayId === "none" ? "none" : (cite?.status ?? "invalid");

  if (status === "none") {
    return (
      <span
        title="The model flagged this statement as having no supporting excerpt."
        className="mx-0.5 text-xs text-gray-400"
      >
        (uncited)
      </span>
    );
  }
  if (status === "invalid") {
    return (
      <span
        title="This citation could not be verified against the retrieved code."
        className="mx-0.5 text-xs text-red-500"
      >
        [unverified]
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={() => onClick(displayId)}
      title="Show the cited code"
      className="mx-0.5 rounded bg-blue-50 px-1 text-xs font-medium text-blue-700 hover:bg-blue-100"
    >
      {displayId}
    </button>
  );
}

function ChunkView({
  chunk,
  permalink,
}: {
  chunk: CitedChunkItem;
  permalink: string | null;
}) {
  return (
    <section className="space-y-2 rounded border border-blue-200 bg-blue-50/40 p-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-xs text-gray-700">
          {chunk.file_path}:{chunk.start_line}-{chunk.end_line}
          {chunk.name ? ` · ${chunk.name}` : ""}
        </span>
        {permalink && (
          <a
            href={permalink}
            target="_blank"
            rel="noopener noreferrer"
            className="shrink-0 text-xs font-medium text-blue-700 hover:underline"
          >
            Open on GitHub ↗
          </a>
        )}
      </div>
      <div className="overflow-hidden rounded border border-gray-200">
        <Editor
          height="220px"
          language={MONACO_LANG[chunk.language ?? ""] ?? "plaintext"}
          value={chunk.content}
          theme="vs-dark"
          options={{
            readOnly: true,
            domReadOnly: true,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            fontSize: 12,
            lineNumbers: (n) => String(chunk.start_line + n - 1),
          }}
        />
      </div>
    </section>
  );
}

function SourcesPanel({
  sources,
  citations,
  expanded,
  onToggle,
}: {
  sources: CitedChunkItem[];
  citations: Map<string, ResolvedCitation> | null;
  expanded: string | null;
  onToggle: (displayId: string) => void;
}) {
  return (
    <section className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
        Sources ({sources.length} retrieved)
      </h3>
      <ul className="space-y-1">
        {sources.map((s) => {
          const cited = citations?.get(s.display_id)?.status === "valid";
          return (
            <li key={s.display_id}>
              <button
                type="button"
                onClick={() => onToggle(s.display_id)}
                className={`flex w-full items-baseline justify-between gap-2 rounded border px-3 py-1.5 text-left text-xs transition-colors hover:bg-gray-50 ${
                  expanded === s.display_id
                    ? "border-blue-300 bg-blue-50"
                    : "border-gray-200"
                }`}
              >
                <span className="font-mono text-gray-700">
                  <span className="mr-1 rounded bg-gray-100 px-1 font-medium">
                    {s.display_id}
                  </span>
                  {s.file_path}:{s.start_line}-{s.end_line}
                  {cited && <span className="ml-1 text-blue-600">· cited</span>}
                </span>
                <span className="shrink-0 tabular-nums text-gray-400">
                  {s.similarity.toFixed(3)}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
