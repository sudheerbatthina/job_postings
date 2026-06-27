const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

export async function submitResume({
  file,
  location,
  isRemote,
  resultLimit = 10,
  sortMode = "most_recent",
  freshnessWindowMinutes = 10,
}) {
  const form = new FormData();
  if (file) form.append("resume", file);
  form.append("location", location);
  form.append("is_remote", isRemote);
  form.append("result_limit", resultLimit);
  form.append("sort_mode", sortMode);
  form.append("freshness_window_minutes", freshnessWindowMinutes);

  const res = await fetch(`${API_URL}/api/analyze`, { method: "POST", body: form });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const err = new Error(data.detail || "Upload failed");
    err.status = res.status;
    throw err;
  }
  return res.json(); // { job_id }
}

export async function getJobStatus(jobId) {
  const res = await fetch(`${API_URL}/api/analyze/${jobId}`);
  if (!res.ok) throw new Error((await res.json()).detail || "Could not fetch status");
  return res.json(); // { status, message, results, error }
}

export async function getStoredResume() {
  const res = await fetch(`${API_URL}/api/resume`);
  if (!res.ok) throw new Error("Could not fetch stored resume");
  return res.json(); // { stored: bool, filename?, keywords?, email?, stored_at? }
}

export function exportUrl(jobId) {
  return `${API_URL}/api/analyze/${jobId}/export.xlsx`;
}
