import type { ReposSummaryResponse } from "@repowise-dev/types/repos";
import { apiGet, apiPost, apiPatch, apiDelete } from "./client";
import type {
  CloneTaskResponse,
  RemoteRepoCreate,
  RepoCreate,
  RepoUpdate,
  RepoResponse,
  JobResponse,
  RepoStatsResponse,
  PreflightResponse,
  GenerateRequest,
  GenerateEstimate,
  JobLaunchResponse,
} from "./types";

export async function listRepos(): Promise<RepoResponse[]> {
  return apiGet<RepoResponse[]>("/api/repos");
}

/** One-call payload for the multi-repo dashboard: every registered repo's
 *  headline figures. Replaces `listRepos` plus a `getRepoStats` and a
 *  `getGitSummary` per repo, whose cost grew with the repository count. */
export async function getReposSummary(): Promise<ReposSummaryResponse> {
  return apiGet<ReposSummaryResponse>("/api/repos/summary");
}

export async function getRepo(repoId: string): Promise<RepoResponse> {
  return apiGet<RepoResponse>(`/api/repos/${repoId}`);
}

export async function createRepo(data: RepoCreate): Promise<RepoResponse> {
  return apiPost<RepoResponse>("/api/repos", data);
}

/** Add a repository by URL; the server clones it onto its own disk.
 *
 * Returns immediately with a clone handle rather than the repository — a
 * clone outlives an HTTP request. Poll {@link getRemoteClone} until
 * `repo_id` is set, or use {@link waitForClone}. */
export async function createRepoFromRemote(
  data: RemoteRepoCreate,
): Promise<CloneTaskResponse> {
  return apiPost<CloneTaskResponse>("/api/repos/remote", data);
}

/** Current progress of a clone started by {@link createRepoFromRemote}. */
export async function getRemoteClone(cloneId: string): Promise<CloneTaskResponse> {
  return apiGet<CloneTaskResponse>(`/api/repos/remote/${cloneId}`);
}

/** Poll a clone to completion.
 *
 * Resolves with the finished task (`status` `"completed"`) or rejects with
 * the server's error message. `onProgress` receives every poll so the caller
 * can show what the clone is doing. */
export async function waitForClone(
  cloneId: string,
  options: {
    onProgress?: (task: CloneTaskResponse) => void;
    intervalMs?: number;
    signal?: AbortSignal;
  } = {},
): Promise<CloneTaskResponse> {
  const { onProgress, intervalMs = 1500, signal } = options;
  for (;;) {
    if (signal?.aborted) throw new Error("Clone polling cancelled");
    const task = await getRemoteClone(cloneId);
    onProgress?.(task);
    if (task.status === "completed") return task;
    if (task.status === "failed") {
      throw new Error(task.error || `Could not add ${task.slug}`);
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

export async function updateRepo(repoId: string, data: RepoUpdate): Promise<RepoResponse> {
  return apiPatch<RepoResponse>(`/api/repos/${repoId}`, data);
}

export async function syncRepo(repoId: string): Promise<JobResponse> {
  return apiPost<JobResponse>(`/api/repos/${repoId}/sync`);
}

export async function fullResyncRepo(repoId: string): Promise<JobResponse> {
  return apiPost<JobResponse>(`/api/repos/${repoId}/full-resync`);
}

/** Start the first full index (docs included) for a registered repo.
 * Returns 409 when a job is already active for it. */
export async function startIndexJob(
  repoId: string,
): Promise<{ job_id: string; status: string }> {
  return apiPost<{ job_id: string; status: string }>(`/api/repos/${repoId}/index`);
}

/** Provider connectivity smoke test + page/cost estimate for a first index. */
export async function preflightIndex(
  repoId: string,
  coveragePct?: number,
): Promise<PreflightResponse> {
  return apiPost<PreflightResponse>(
    `/api/repos/${repoId}/preflight`,
    undefined,
    undefined,
    coveragePct !== undefined ? { coverage_pct: coveragePct } : undefined,
  );
}

/** Cost + page counts for a generate selection, cascade fallout included.
 *  Heavy (rehydrates the graph and re-parses), so fetch it lazily — never on
 *  every render. */
export async function generateEstimate(
  repoId: string,
  body: GenerateRequest,
): Promise<GenerateEstimate> {
  return apiPost<GenerateEstimate>(`/api/repos/${repoId}/generate/estimate`, body);
}

/** Launch a scoped generate job (writes the selected pages with a model).
 *  Returns the job id + a short-lived stream token to watch progress. */
export async function generatePages(
  repoId: string,
  body: GenerateRequest,
): Promise<JobLaunchResponse> {
  return apiPost<JobLaunchResponse>(`/api/repos/${repoId}/generate`, body);
}

export async function deleteRepo(repoId: string): Promise<{ ok: boolean; deleted_pages: number }> {
  return apiDelete<{ ok: boolean; deleted_pages: number }>(`/api/repos/${repoId}`);
}

export async function getRepoStats(repoId: string): Promise<RepoStatsResponse> {
  return apiGet<RepoStatsResponse>(`/api/repos/${repoId}/stats`);
}
