const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

export async function submitResume({ file, location, isRemote, hoursOld }) {
  const form = new FormData();
  form.append("resume", file);
  form.append("location", location);
  form.append("is_remote", isRemote);
  form.append("hours_old", hoursOld);

  const res = await fetch(`${API_URL}/api/analyze`, { method: "POST", body: form });
  if (!res.ok) throw new Error((await res.json()).detail || "Upload failed");
  return res.json(); // { job_id }
}

export async function getJobStatus(jobId) {
  const res = await fetch(`${API_URL}/api/analyze/${jobId}`);
  if (!res.ok) throw new Error((await res.json()).detail || "Could not fetch status");
  return res.json(); // { status, message, results, error }
}

export function exportUrl(jobId) {
  return `${API_URL}/api/analyze/${jobId}/export.xlsx`;
}
