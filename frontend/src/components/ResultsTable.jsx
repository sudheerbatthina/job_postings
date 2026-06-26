import { useMemo, useState } from "react";
import { ExternalLink, Download, RotateCcw, ChevronDown, ChevronUp } from "lucide-react";
import { exportUrl } from "../api";

function scoreTier(score) {
  if (score >= 70) return { bg: "bg-teal-50", text: "text-teal-800", ring: "ring-teal-700/30" };
  if (score >= 40) return { bg: "bg-amber-50", text: "text-amber-800", ring: "ring-amber-700/30" };
  return { bg: "bg-stone-100", text: "text-stone-600", ring: "ring-stone-400/30" };
}

function relativeTime(dateStr) {
  if (!dateStr) return "Unknown date";
  const hasTime = dateStr.includes("T");
  const diff = Date.now() - new Date(dateStr).getTime();
  if (hasTime) {
    const hours = Math.floor(diff / 3600000);
    if (hours < 1) return "Just now";
    if (hours < 24) return `${hours}h ago`;
  }
  const days = Math.floor(diff / 86400000);
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

function bandLabel(band) {
  if (band === "strong") return "Strong";
  if (band === "good") return "Good";
  if (band === "broader") return "Broader";
  return "Match";
}

function resultHeading(results, showBroader) {
  if (showBroader) return "broader";
  const allStrong = results.length > 0 && results.every((job) => job.match_band === "strong");
  return allStrong ? "strong" : "good AI/ML";
}

const SORT_OPTIONS = [
  { value: "recommended", label: "Recommended" },
  { value: "top_matched", label: "Top Matched" },
  { value: "most_recent", label: "Most Recent" },
];

function applySort(jobs, sortBy) {
  if (sortBy === "top_matched") {
    return [...jobs].sort((a, b) => (b.ats_score ?? 0) - (a.ats_score ?? 0));
  }
  if (sortBy === "most_recent") {
    return [...jobs].sort((a, b) => {
      const ta = a.posted_at_ts ? new Date(a.posted_at_ts).getTime() : 0;
      const tb = b.posted_at_ts ? new Date(b.posted_at_ts).getTime() : 0;
      return tb - ta;
    });
  }
  return jobs; // "recommended" — backend order preserved
}

export default function ResultsTable({ results, lowConfidenceResults = [], jobId, onReset }) {
  const [minScore, setMinScore] = useState(65);
  const [remoteOnly, setRemoteOnly] = useState(false);
  const [sortBy, setSortBy] = useState("recommended");
  const [expandedUrl, setExpandedUrl] = useState(null);
  const [showBroader, setShowBroader] = useState(false);
  const visibleResults = useMemo(
    () => (showBroader ? [...results, ...lowConfidenceResults] : results),
    [showBroader, results, lowConfidenceResults]
  );

  const filtered = useMemo(() => {
    const base = visibleResults
      .filter((r) => (r.ats_score ?? 0) >= minScore)
      .filter((r) => !remoteOnly || r.is_remote);
    return applySort(base, sortBy);
  }, [visibleResults, minScore, remoteOnly, sortBy]);

  if (results.length === 0 && lowConfidenceResults.length === 0) {
    return (
      <div className="w-full max-w-xl mx-auto text-center py-16">
        <p className="text-stone-700">No strong AI/ML matches from the last 30 hours.</p>
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
          {filtered.length} {resultHeading(filtered, showBroader)} match{filtered.length === 1 ? "" : "es"}
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
          ATS match
          <input
            type="range"
            min="0"
            max="90"
            step="5"
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
        <select
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value)}
          className="text-sm text-stone-600 border border-stone-200 rounded-md px-2 py-1 bg-white focus:outline-none focus:ring-1 focus:ring-teal-600"
        >
          {SORT_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      </div>

      {lowConfidenceResults.length > 0 && (
        <div className="mt-3">
          <button
            onClick={() => {
              if (!showBroader) setMinScore(0);
              setShowBroader((value) => !value);
            }}
            className="text-sm font-medium text-teal-700 hover:underline"
          >
            {showBroader ? "Hide broader matches" : "Show broader matches"}
          </button>
        </div>
      )}

      {results.length === 0 && lowConfidenceResults.length > 0 && !showBroader && (
        <div className="w-full max-w-xl mx-auto text-center py-12">
          <p className="text-stone-700">No strong AI/ML matches from the last 30 hours.</p>
          <p className="mt-1 text-sm text-stone-500">Try lowering the score filter or checking broader matches.</p>
        </div>
      )}

      <ul className="mt-4 flex flex-col gap-2">
        {filtered.map((job) => {
          const tier = scoreTier(job.ats_score);
          const salary = salaryLabel(job.min_amount, job.max_amount);
          const missing = job.missing_keywords || [];
          const isExpanded = expandedUrl === job.job_url;
          const applyHref = job.apply_url || job.job_url;
          const meta = [
            job.company,
            job.location,
            job.posted_age_label || relativeTime(job.date_posted),
            job.applicants_label,
            salary,
          ].filter(Boolean);
          const sourceLabel = job.source_type || job.source;
          return (
            <li
              key={job.job_url}
              className="rounded-lg border border-stone-200 bg-white"
            >
              <div className="flex items-center gap-4 px-4 py-3">
                <div
                  className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-full ring-1 ${tier.bg} ${tier.ring}`}
                >
                  <span className={`font-mono font-bold text-base ${tier.text}`}>{job.ats_score}</span>
                </div>

                <div className="flex-1 min-w-0">
                  <p className="font-medium text-stone-900 truncate">{job.title}</p>
                  <p className="text-sm text-stone-500 truncate">
                    {meta.join(" · ")}
                  </p>
                  <p className="text-xs text-stone-400 truncate">
                    {bandLabel(job.match_band)} match
                    {sourceLabel ? ` · ${sourceLabel}` : ""}
                  </p>
                  {showBroader && job.exclude_reason && (
                    <p className="text-xs text-amber-700 truncate">{job.exclude_reason}</p>
                  )}
                </div>

                <div className="flex items-center gap-2 shrink-0">
                  {missing.length > 0 && (
                    <button
                      onClick={() => setExpandedUrl(isExpanded ? null : job.job_url)}
                      className="flex items-center gap-1 text-xs text-stone-400 hover:text-stone-600"
                      title="Why this score?"
                    >
                      {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                      Why?
                    </button>
                  )}
                  <a
                    href={applyHref}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-1.5 rounded-md bg-stone-900 px-3 py-2 text-sm font-medium text-white hover:bg-stone-700"
                  >
                    Apply <ExternalLink size={13} />
                  </a>
                </div>
              </div>

              {isExpanded && missing.length > 0 && (
                <div className="border-t border-stone-100 px-4 pb-3 pt-2">
                  <p className="mb-2 text-xs font-medium text-stone-500">Missing keywords</p>
                  <div className="flex flex-wrap gap-1.5">
                    {missing.map((kw) => (
                      <span
                        key={kw}
                        className="rounded-full bg-red-50 px-2.5 py-0.5 text-xs font-medium text-red-700 ring-1 ring-red-200"
                      >
                        {kw}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
