import { useMemo, useState } from "react";
import { ExternalLink, Download, RotateCcw } from "lucide-react";
import { exportUrl } from "../api";

function scoreTier(score) {
  if (score >= 70) return { bg: "bg-teal-50", text: "text-teal-800", ring: "ring-teal-700/30" };
  if (score >= 40) return { bg: "bg-amber-50", text: "text-amber-800", ring: "ring-amber-700/30" };
  return { bg: "bg-stone-100", text: "text-stone-600", ring: "ring-stone-400/30" };
}

function relativeDate(dateStr) {
  if (!dateStr) return "Unknown date";
  const days = Math.floor((Date.now() - new Date(dateStr).getTime()) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  return `${days} days ago`;
}

function salaryLabel(min, max) {
  if (!min && !max) return null;
  const fmt = (n) => `$${Math.round(n / 1000)}k`;
  if (min && max) return `${fmt(min)}–${fmt(max)}`;
  return fmt(min || max);
}

export default function ResultsTable({ results, jobId, onReset }) {
  const [minScore, setMinScore] = useState(0);
  const [remoteOnly, setRemoteOnly] = useState(false);

  const filtered = useMemo(
    () =>
      results
        .filter((r) => (r.claude_score ?? 0) >= minScore)
        .filter((r) => !remoteOnly || r.is_remote),
    [results, minScore, remoteOnly]
  );

  if (results.length === 0) {
    return (
      <div className="w-full max-w-xl mx-auto text-center py-16">
        <p className="text-stone-700">No matching jobs found right now.</p>
        <p className="mt-1 text-sm text-stone-500">The job boards may not have new postings yet — try again in a few hours.</p>
        <button onClick={onReset} className="mt-6 text-teal-700 font-medium hover:underline">
          Try another search
        </button>
      </div>
    );
  }

  return (
    <div className="w-full max-w-3xl mx-auto">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="text-xl font-semibold text-stone-900">
          {filtered.length} matching role{filtered.length === 1 ? "" : "s"}
        </h2>
        <div className="flex items-center gap-3">
          <a
            href={exportUrl(jobId)}
            className="flex items-center gap-1.5 text-sm font-medium text-teal-700 hover:underline"
          >
            <Download size={15} /> Export .xlsx
          </a>
          <button
            onClick={onReset}
            className="flex items-center gap-1.5 text-sm font-medium text-stone-500 hover:text-stone-800"
          >
            <RotateCcw size={14} /> New search
          </button>
        </div>
      </div>

      <div className="mt-4 flex items-center gap-6 rounded-lg bg-white border border-stone-200 px-4 py-3">
        <label className="flex items-center gap-2 text-sm text-stone-600 flex-1">
          Min score
          <input
            type="range"
            min="0"
            max="90"
            step="10"
            value={minScore}
            onChange={(e) => setMinScore(Number(e.target.value))}
            className="flex-1 accent-teal-700"
          />
          <span className="font-mono text-stone-800 w-8">{minScore}</span>
        </label>
        <label className="flex items-center gap-2 text-sm text-stone-600">
          <input
            type="checkbox"
            checked={remoteOnly}
            onChange={(e) => setRemoteOnly(e.target.checked)}
            className="h-4 w-4 rounded border-stone-300 text-teal-700"
          />
          Remote only
        </label>
      </div>

      <ul className="mt-4 flex flex-col gap-2">
        {filtered.map((job) => {
          const tier = scoreTier(job.claude_score);
          const salary = salaryLabel(job.min_amount, job.max_amount);
          return (
            <li
              key={job.job_url}
              className="flex items-center gap-4 rounded-lg border border-stone-200 bg-white px-4 py-3"
            >
              <div
                className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-full ring-1 ${tier.bg} ${tier.ring}`}
              >
                <span className={`font-mono font-bold text-base ${tier.text}`}>{job.claude_score}</span>
              </div>

              <div className="flex-1 min-w-0">
                <p className="font-medium text-stone-900 truncate">{job.title}</p>
                <p className="text-sm text-stone-500 truncate">
                  {job.company} · {job.location} · {relativeDate(job.date_posted)}
                  {salary ? ` · ${salary}` : ""}
                </p>
              </div>

              <a
                href={job.job_url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1.5 shrink-0 rounded-md bg-stone-900 px-3 py-2 text-sm font-medium text-white hover:bg-stone-700"
              >
                Apply <ExternalLink size={13} />
              </a>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
