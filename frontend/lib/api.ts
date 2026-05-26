// Types mirror the backend's Pydantic schemas in app/schemas.py.
// Hand-written for Slice 1; auto-gen later if the API surface grows.

export type FileOut = {
  path: string;
  size_bytes: number;
  language: string | null;
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
