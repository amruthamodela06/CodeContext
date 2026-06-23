// Types mirror the backend's Pydantic schemas in app/schemas.py.
// Hand-written for Slice 1/2; auto-gen later if the API surface grows.

export type FileOut = {
  path: string;
  size_bytes: number;
  language: string | null;
  chunk_count: number;
};

export type RepoOut = {
  id: number;
  owner: string;
  name: string;
  default_branch: string;
  commit_sha: string | null;
  ingested_at: string;
};

export type RepoFilesResponse = {
  repo: RepoOut;
  files: FileOut[];
  file_count: number;
};

export type ChunkOut = {
  id: number;
  file_id: number;
  chunk_type: string;
  name: string | null;
  parent_name: string | null;
  start_line: number;
  end_line: number;
  language: string;
  is_async: boolean;
  extra_metadata: Record<string, unknown>;
  content: string;
  created_at: string;
};

export type ChunkSummary = {
  by_type: Record<string, number>;
  by_language: Record<string, number>;
};

export type RepoChunksResponse = {
  repo_id: number;
  chunks: ChunkOut[];
  total: number;
  limit: number;
  offset: number;
  summary: ChunkSummary;
};

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function unwrap<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export async function ingestRepo(url: string): Promise<RepoFilesResponse> {
  const res = await fetch(`${API_URL}/ingest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  return unwrap<RepoFilesResponse>(res);
}

// Load an already-ingested repo without re-cloning. Used by the
// localStorage-restore path so iterating doesn't wipe + re-embed each refresh.
export async function fetchRepoFiles(
  owner: string,
  name: string,
): Promise<RepoFilesResponse> {
  const res = await fetch(`${API_URL}/repos/${owner}/${name}/files`);
  return unwrap<RepoFilesResponse>(res);
}

export async function fetchChunks(
  repoId: number,
  opts: { limit?: number; offset?: number } = {},
): Promise<RepoChunksResponse> {
  const params = new URLSearchParams();
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts.offset !== undefined) params.set("offset", String(opts.offset));
  const qs = params.toString();
  const res = await fetch(
    `${API_URL}/repos/${repoId}/chunks${qs ? `?${qs}` : ""}`,
  );
  return unwrap<RepoChunksResponse>(res);
}

// --- Embeddings + search (Slice 3) ---

export type EmbedTrigger = {
  repo_id: number;
  embedding_status: string;
};

export type EmbeddingStatus = {
  repo_id: number;
  embedding_status: "pending" | "in_progress" | "done" | "failed";
  embedding_progress: number;
  chunks_total: number;
  chunks_embedded: number;
};

export type SearchResultItem = {
  chunk_id: number;
  similarity: number;
  file_path: string;
  chunk_type: string;
  name: string | null;
  start_line: number;
  end_line: number;
  language: string;
  content: string;
};

export type SearchResponse = {
  repo_id: number;
  query: string;
  results: SearchResultItem[];
};

export async function triggerEmbed(repoId: number): Promise<EmbedTrigger> {
  const res = await fetch(`${API_URL}/repos/${repoId}/embed`, { method: "POST" });
  return unwrap<EmbedTrigger>(res);
}

export async function fetchEmbeddingStatus(
  repoId: number,
): Promise<EmbeddingStatus> {
  const res = await fetch(`${API_URL}/repos/${repoId}/embedding-status`);
  return unwrap<EmbeddingStatus>(res);
}

export async function search(
  repoId: number,
  query: string,
  topK = 5,
): Promise<SearchResponse> {
  const res = await fetch(`${API_URL}/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo_id: repoId, query, top_k: topK }),
  });
  return unwrap<SearchResponse>(res);
}

// --- Cited Q&A / streaming query (Slice 4, ADR 0010) ---

export type CitedChunkItem = {
  // Slice 5: discriminator added so CitedChunk/Commit/PR/Issue form a tagged
  // union the QueryPanel can branch on. Optional so older JSON payloads
  // (Slice 4 servers) still parse -- the backend always sends 'chunk' now.
  type?: "chunk";
  display_id: string;
  chunk_id: number;
  file_path: string;
  start_line: number;
  end_line: number;
  language: string | null;
  chunk_type: string;
  name: string | null;
  content: string;
  similarity: number;
  // Slice 5h fix: backend now attaches a permalink to every retrieved
  // entity (not just cited ones) so Sources-panel rows always have an
  // Open-on-GitHub link.
  permalink?: string;
};

export type CitationStatus = "valid" | "none" | "invalid";
export type EntityType = "chunk" | "commit" | "pr" | "issue";

// --- Slice 5: typed citable entities (commits / PRs / issues) ---

export type CitedCommitItem = {
  type: "commit";
  display_id: string;
  commit_id: number;
  sha: string;
  author_name: string | null;
  authored_at: string | null;
  message: string;
  similarity: number;
  permalink?: string;
};

export type CitedPRItem = {
  type: "pr";
  display_id: string;
  pr_id: number;
  number: number;
  title: string;
  body: string | null;
  state: string;
  merged_at: string | null;
  similarity: number;
  permalink?: string;
};

export type CitedIssueItem = {
  type: "issue";
  display_id: string;
  issue_id: number;
  number: number;
  title: string;
  body: string | null;
  state: string;
  closed_at: string | null;
  similarity: number;
  permalink?: string;
};

export type TypedSources = {
  chunks: CitedChunkItem[];
  commits: CitedCommitItem[];
  prs: CitedPRItem[];
  issues: CitedIssueItem[];
};

// Slice 5g: trace payload carrying classifier + multi-hop debug info.
export type QueryTrace = {
  classifier?: {
    method: string;
    confidence: number;
    fallback_used: boolean;
  };
  category?: string;
  seed_chunk_ids?: number[];
  expansion_candidates?: number;
  reranked_count?: number;
};

export type ResolvedCitation = {
  entity_type: EntityType;
  display_id: string;
  status: CitationStatus;
  // Type-specific (only one set per row).
  chunk_id: number | null;
  commit_sha: string | null;
  pr_number: number | null;
  issue_number: number | null;
  // Shared display fields.
  file_path: string | null;
  start_line: number | null;
  end_line: number | null;
  title: string | null;
  permalink: string | null;
};

export type QueryStreamHandlers = {
  onSources?: (sources: TypedSources) => void;
  onToken?: (text: string) => void;
  onCitations?: (
    citations: ResolvedCitation[],
    warnings: string[],
    trace: QueryTrace,
  ) => void;
  onError?: (message: string, stage: string) => void;
  onDone?: () => void;
  signal?: AbortSignal;
};

const SSE_FRAME_SEP = /\r?\n\r?\n/;

function dispatchSseFrame(frame: string, handlers: QueryStreamHandlers): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith(":")) continue; // comment / keep-alive ping
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (dataLines.length === 0) return;
  const raw = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
  switch (event) {
    case "sources":
      // Slice 5g: 'sources' payload is now {chunks, commits, prs, issues}.
      handlers.onSources?.({
        chunks: (raw.chunks as CitedChunkItem[]) ?? [],
        commits: (raw.commits as CitedCommitItem[]) ?? [],
        prs: (raw.prs as CitedPRItem[]) ?? [],
        issues: (raw.issues as CitedIssueItem[]) ?? [],
      });
      break;
    case "token":
      handlers.onToken?.(raw.text as string);
      break;
    case "citations":
      handlers.onCitations?.(
        raw.citations as ResolvedCitation[],
        (raw.warnings as string[]) ?? [],
        (raw.trace as QueryTrace) ?? {},
      );
      break;
    case "error":
      handlers.onError?.(raw.message as string, raw.stage as string);
      break;
    case "done":
      handlers.onDone?.();
      break;
  }
}

// POST + ReadableStream (not EventSource — that's GET-only). Frontend talks to
// :8000 directly via NEXT_PUBLIC_API_URL, so there's no buffering proxy hop.
export async function streamQuery(
  repoId: number,
  question: string,
  topK = 5,
  handlers: QueryStreamHandlers = {},
): Promise<void> {
  const res = await fetch(`${API_URL}/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo_id: repoId, question, top_k: topK, stream: true }),
    signal: handlers.signal,
  });
  if (!res.ok || !res.body) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `Request failed (${res.status})`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let match: RegExpExecArray | null;
      while ((match = SSE_FRAME_SEP.exec(buffer)) !== null) {
        const frame = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        dispatchSseFrame(frame, handlers);
      }
    }
  } finally {
    reader.releaseLock();
  }
}
