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
  type CitedCommitItem,
  type CitedIssueItem,
  type CitedPRItem,
  type EntityType,
  type QueryTrace,
  type ResolvedCitation,
  type TypedSources,
} from "@/lib/api";

type Phase = "idle" | "streaming" | "done" | "error";

// Slice 5f: token format extended to [(chunk|commit|pr|issue):...].
const TOKEN_RE = /\[(chunk|commit|pr|issue):(none|[A-Za-z0-9_-]{1,16})\]/g;

const MONACO_LANG: Record<string, string> = {
  Python: "python",
  TypeScript: "typescript",
  JavaScript: "javascript",
  Go: "go",
  Rust: "rust",
};

// Per-type styling for citation chips so users can tell types apart at a glance.
const CHIP_STYLE: Record<EntityType, string> = {
  chunk: "bg-blue-50 text-blue-700 hover:bg-blue-100",
  commit: "bg-emerald-50 text-emerald-700 hover:bg-emerald-100",
  pr: "bg-violet-50 text-violet-700 hover:bg-violet-100",
  issue: "bg-amber-50 text-amber-800 hover:bg-amber-100",
};

const TYPE_ICON: Record<EntityType, string> = {
  chunk: "◧",
  commit: "◆",
  pr: "⇄",
  issue: "●",
};

type CitedEntity = CitedChunkItem | CitedCommitItem | CitedPRItem | CitedIssueItem;
type ExpandedKey = `${EntityType}:${string}` | null;

const EMPTY_SOURCES: TypedSources = { chunks: [], commits: [], prs: [], issues: [] };

export function QueryPanel({ repoId }: { repoId: number }) {
  const [question, setQuestion] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [answer, setAnswer] = useState("");
  const [sources, setSources] = useState<TypedSources>(EMPTY_SOURCES);
  const [citations, setCitations] = useState<Map<string, ResolvedCitation> | null>(
    null,
  );
  const [warnings, setWarnings] = useState<string[]>([]);
  const [trace, setTrace] = useState<QueryTrace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<ExpandedKey>(null);
  const [showDebug, setShowDebug] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // Index every cited entity by its (type, display_id) for chip expansion.
  const entityIndex = useMemo(() => {
    const map = new Map<string, CitedEntity>();
    for (const c of sources.chunks) map.set(`chunk:${c.display_id}`, c);
    for (const c of sources.commits) map.set(`commit:${c.display_id}`, c);
    for (const c of sources.prs) map.set(`pr:${c.display_id}`, c);
    for (const c of sources.issues) map.set(`issue:${c.display_id}`, c);
    return map;
  }, [sources]);

  // Citations keyed by (type, display_id) too, for chip status lookup.
  const citationIndex = useMemo(() => {
    if (!citations) return null;
    const map = new Map<string, ResolvedCitation>();
    citations.forEach((c) => map.set(`${c.entity_type}:${c.display_id}`, c));
    return map;
  }, [citations]);

  const onSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      setPhase("streaming");
      setAnswer("");
      setSources(EMPTY_SOURCES);
      setCitations(null);
      setWarnings([]);
      setTrace(null);
      setError(null);
      setExpanded(null);

      try {
        await streamQuery(repoId, question, 5, {
          signal: controller.signal,
          onSources: setSources,
          onToken: (text) => setAnswer((prev) => prev + text),
          onCitations: (cites, warns, t) => {
            // Key by (entity_type, display_id) so types don't collide.
            const map = new Map<string, ResolvedCitation>();
            for (const c of cites) {
              map.set(`${c.entity_type}:${c.display_id}`, c);
            }
            setCitations(map);
            setWarnings(warns);
            setTrace(t);
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

  const toggleExpand = useCallback((key: ExpandedKey) => {
    setExpanded((cur) => (cur === key ? null : key));
  }, []);

  const rendered = useMemo(
    () => renderAnswer(answer, citationIndex, toggleExpand),
    [answer, citationIndex, toggleExpand],
  );

  const expandedEntity = expanded ? entityIndex.get(expanded) : undefined;
  const expandedCite = expanded ? citationIndex?.get(expanded) : undefined;

  const totalSources =
    sources.chunks.length +
    sources.commits.length +
    sources.prs.length +
    sources.issues.length;

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
          {trace?.category && (
            <CategoryPill category={trace.category} trace={trace} />
          )}
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

      {expandedEntity && (
        <ExpandedEntityView
          entity={expandedEntity}
          permalink={expandedCite?.permalink ?? null}
        />
      )}

      {totalSources > 0 && (
        <SourcesPanel
          sources={sources}
          citations={citationIndex}
          expanded={expanded}
          onToggle={toggleExpand}
        />
      )}

      {trace && (
        <DebugTrace
          trace={trace}
          showDebug={showDebug}
          onToggle={() => setShowDebug((s) => !s)}
        />
      )}
    </div>
  );
}

// --- Answer rendering with typed chips --------------------------------------

function renderAnswer(
  answer: string,
  citations: Map<string, ResolvedCitation> | null,
  onCite: (key: ExpandedKey) => void,
): ReactNode[] {
  if (!citations) return [answer];
  const nodes: ReactNode[] = [];
  let last = 0;
  let key = 0;
  TOKEN_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = TOKEN_RE.exec(answer)) !== null) {
    if (m.index > last) nodes.push(answer.slice(last, m.index));
    const entityType = m[1] as EntityType;
    const displayId = m[2];
    nodes.push(
      <CitationRef
        key={`c-${key++}`}
        entityType={entityType}
        displayId={displayId}
        cite={citations.get(`${entityType}:${displayId}`)}
        onClick={onCite}
      />,
    );
    last = m.index + m[0].length;
  }
  if (last < answer.length) nodes.push(answer.slice(last));
  return nodes;
}

function CitationRef({
  entityType,
  displayId,
  cite,
  onClick,
}: {
  entityType: EntityType;
  displayId: string;
  cite: ResolvedCitation | undefined;
  onClick: (key: ExpandedKey) => void;
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
        title={`This [${entityType}:${displayId}] citation could not be verified against the retrieved set.`}
        className="mx-0.5 text-xs text-red-500"
      >
        [unverified]
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={() => onClick(`${entityType}:${displayId}`)}
      title={`Show the cited ${entityType}`}
      className={`mx-0.5 rounded px-1 text-xs font-medium ${CHIP_STYLE[entityType]}`}
    >
      <span className="mr-0.5">{TYPE_ICON[entityType]}</span>
      {displayId}
    </button>
  );
}

// --- Category pill ---------------------------------------------------------

function CategoryPill({ category, trace }: { category: string; trace: QueryTrace }) {
  const confidence = trace.classifier?.confidence;
  const method = trace.classifier?.method;
  const label = category.replace("_", " ");
  return (
    <div className="mb-3 flex items-center gap-2 text-xs">
      <span className="rounded-full bg-gray-100 px-2 py-0.5 font-medium text-gray-700">
        {label}
      </span>
      {method && (
        <span className="text-gray-500">
          via {method}
          {confidence !== undefined && ` (${(confidence * 100).toFixed(0)}%)`}
        </span>
      )}
      {trace.classifier?.fallback_used && (
        <span className="text-amber-600">· fallback</span>
      )}
    </div>
  );
}

// --- Per-type expanded views -----------------------------------------------

function ExpandedEntityView({
  entity,
  permalink,
}: {
  entity: CitedEntity;
  permalink: string | null;
}) {
  if (entity.type === "commit") {
    return <CommitView commit={entity} permalink={permalink} />;
  }
  if (entity.type === "pr") {
    return <PRView pr={entity} permalink={permalink} />;
  }
  if (entity.type === "issue") {
    return <IssueView issue={entity} permalink={permalink} />;
  }
  return <ChunkView chunk={entity} permalink={permalink} />;
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
      <ExpandedHeader
        label={`${chunk.file_path}:${chunk.start_line}-${chunk.end_line}${
          chunk.name ? ` · ${chunk.name}` : ""
        }`}
        permalink={permalink}
      />
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

function CommitView({
  commit,
  permalink,
}: {
  commit: CitedCommitItem;
  permalink: string | null;
}) {
  const when = commit.authored_at
    ? new Date(commit.authored_at).toISOString().slice(0, 10)
    : "unknown";
  return (
    <section className="space-y-2 rounded border border-emerald-200 bg-emerald-50/40 p-3">
      <ExpandedHeader
        label={`commit ${commit.sha.slice(0, 7)} · ${commit.author_name ?? "unknown"} · ${when}`}
        permalink={permalink}
      />
      <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-white p-3 font-mono text-xs leading-relaxed text-gray-800">
        {commit.message.trim()}
      </pre>
    </section>
  );
}

function PRView({ pr, permalink }: { pr: CitedPRItem; permalink: string | null }) {
  const when = pr.merged_at
    ? `merged ${new Date(pr.merged_at).toISOString().slice(0, 10)}`
    : pr.state;
  return (
    <section className="space-y-2 rounded border border-violet-200 bg-violet-50/40 p-3">
      <ExpandedHeader
        label={`PR #${pr.number} · ${pr.title} (${when})`}
        permalink={permalink}
      />
      {pr.body && (
        <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-white p-3 font-mono text-xs leading-relaxed text-gray-800">
          {pr.body.trim().slice(0, 1200)}
          {pr.body.length > 1200 ? "…" : ""}
        </pre>
      )}
    </section>
  );
}

function IssueView({
  issue,
  permalink,
}: {
  issue: CitedIssueItem;
  permalink: string | null;
}) {
  const when = issue.closed_at
    ? `closed ${new Date(issue.closed_at).toISOString().slice(0, 10)}`
    : issue.state;
  return (
    <section className="space-y-2 rounded border border-amber-200 bg-amber-50/40 p-3">
      <ExpandedHeader
        label={`Issue #${issue.number} · ${issue.title} (${when})`}
        permalink={permalink}
      />
      {issue.body && (
        <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-white p-3 font-mono text-xs leading-relaxed text-gray-800">
          {issue.body.trim().slice(0, 1200)}
          {issue.body.length > 1200 ? "…" : ""}
        </pre>
      )}
    </section>
  );
}

function ExpandedHeader({
  label,
  permalink,
}: {
  label: string;
  permalink: string | null;
}) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="font-mono text-xs text-gray-700">{label}</span>
      {permalink && (
        <a
          href={permalink}
          target="_blank"
          rel="noopener noreferrer"
          className="shrink-0 text-xs font-medium text-gray-700 hover:underline"
        >
          Open on GitHub ↗
        </a>
      )}
    </div>
  );
}

// --- Typed Sources panel ----------------------------------------------------

function SourcesPanel({
  sources,
  citations,
  expanded,
  onToggle,
}: {
  sources: TypedSources;
  citations: Map<string, ResolvedCitation> | null;
  expanded: ExpandedKey;
  onToggle: (key: ExpandedKey) => void;
}) {
  return (
    <section className="space-y-3">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
        Sources retrieved
      </h3>
      {sources.chunks.length > 0 && (
        <SourceGroup
          title="Code chunks"
          type="chunk"
          items={sources.chunks.map((c) => ({
            key: `chunk:${c.display_id}`,
            display_id: c.display_id,
            primary: `${c.file_path}:${c.start_line}-${c.end_line}`,
            secondary: c.name ?? c.chunk_type,
            similarity: c.similarity,
          }))}
          citations={citations}
          expanded={expanded}
          onToggle={onToggle}
        />
      )}
      {sources.commits.length > 0 && (
        <SourceGroup
          title="Commits"
          type="commit"
          items={sources.commits.map((c) => ({
            key: `commit:${c.display_id}`,
            display_id: c.display_id,
            primary: `${c.sha.slice(0, 7)} ${truncate(c.message.split("\n")[0], 60)}`,
            secondary: c.author_name ?? "unknown",
            similarity: c.similarity,
          }))}
          citations={citations}
          expanded={expanded}
          onToggle={onToggle}
        />
      )}
      {sources.prs.length > 0 && (
        <SourceGroup
          title="Pull requests"
          type="pr"
          items={sources.prs.map((p) => ({
            key: `pr:${p.display_id}`,
            display_id: p.display_id,
            primary: `#${p.number} ${truncate(p.title, 60)}`,
            secondary: p.state,
            similarity: p.similarity,
          }))}
          citations={citations}
          expanded={expanded}
          onToggle={onToggle}
        />
      )}
      {sources.issues.length > 0 && (
        <SourceGroup
          title="Issues"
          type="issue"
          items={sources.issues.map((i) => ({
            key: `issue:${i.display_id}`,
            display_id: i.display_id,
            primary: `#${i.number} ${truncate(i.title, 60)}`,
            secondary: i.state,
            similarity: i.similarity,
          }))}
          citations={citations}
          expanded={expanded}
          onToggle={onToggle}
        />
      )}
    </section>
  );
}

type SourceRow = {
  key: string;
  display_id: string;
  primary: string;
  secondary: string;
  similarity: number;
};

function SourceGroup({
  title,
  type,
  items,
  citations,
  expanded,
  onToggle,
}: {
  title: string;
  type: EntityType;
  items: SourceRow[];
  citations: Map<string, ResolvedCitation> | null;
  expanded: ExpandedKey;
  onToggle: (key: ExpandedKey) => void;
}) {
  return (
    <div>
      <h4 className="mb-1 flex items-center gap-1 text-xs font-medium text-gray-600">
        <span>{TYPE_ICON[type]}</span>
        <span>{title}</span>
        <span className="text-gray-400">({items.length})</span>
      </h4>
      <ul className="space-y-1">
        {items.map((it) => {
          const cited = citations?.get(it.key)?.status === "valid";
          return (
            <li key={it.key}>
              <button
                type="button"
                onClick={() => onToggle(it.key as ExpandedKey)}
                className={`flex w-full items-baseline justify-between gap-2 rounded border px-3 py-1.5 text-left text-xs transition-colors hover:bg-gray-50 ${
                  expanded === it.key
                    ? "border-gray-400 bg-gray-50"
                    : "border-gray-200"
                }`}
              >
                <span className="font-mono text-gray-700">
                  <span
                    className={`mr-1 rounded px-1 font-medium ${CHIP_STYLE[type]}`}
                  >
                    {it.display_id}
                  </span>
                  {it.primary}
                  <span className="ml-1 text-gray-400">· {it.secondary}</span>
                  {cited && <span className="ml-1 text-blue-600">· cited</span>}
                </span>
                <span className="shrink-0 tabular-nums text-gray-400">
                  {it.similarity.toFixed(3)}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// --- Debug toggle -----------------------------------------------------------

function DebugTrace({
  trace,
  showDebug,
  onToggle,
}: {
  trace: QueryTrace;
  showDebug: boolean;
  onToggle: () => void;
}) {
  return (
    <details
      open={showDebug}
      className="rounded border border-gray-200 p-2 text-xs"
    >
      <summary
        onClick={(e) => {
          e.preventDefault();
          onToggle();
        }}
        className="cursor-pointer select-none text-gray-500 hover:text-gray-700"
      >
        Debug trace (classifier + multi-hop expansion)
      </summary>
      <pre className="mt-2 overflow-x-auto rounded bg-gray-50 p-2 font-mono text-xs leading-relaxed text-gray-700">
        {JSON.stringify(trace, null, 2)}
      </pre>
    </details>
  );
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
