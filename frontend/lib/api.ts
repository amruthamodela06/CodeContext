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
