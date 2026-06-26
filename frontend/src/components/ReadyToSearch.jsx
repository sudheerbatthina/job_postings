import { useState } from "react";
import { FileText, RotateCcw } from "lucide-react";

export default function ReadyToSearch({ storedResume, onSubmit, onReplace }) {
  const [location, setLocation] = useState("United States");
  const [isRemote, setIsRemote] = useState(false);
  const [resultLimit, setResultLimit] = useState(10);

  const searchTitles = storedResume?.search_titles || [];

  const handleSubmit = (e) => {
    e.preventDefault();
    onSubmit({ location, isRemote, resultLimit });
  };

  return (
    <form onSubmit={handleSubmit} className="w-full max-w-xl mx-auto">
      <h1 className="text-2xl font-semibold tracking-tight text-stone-900">
        Ready to search
      </h1>

      <div className="mt-4 flex items-center gap-2 text-stone-600">
        <FileText size={16} className="shrink-0 text-teal-700" />
        <span className="text-sm">Using saved resume: <span className="font-medium text-stone-800">{storedResume?.filename}</span></span>
      </div>

      {searchTitles.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-2">
          {searchTitles.map((title) => (
            <span
              key={title}
              className="rounded-full bg-teal-50 px-3 py-1 text-xs font-medium text-teal-800 ring-1 ring-teal-700/20"
            >
              {title}
            </span>
          ))}
        </div>
      )}

      <div className="mt-8 grid grid-cols-2 gap-4">
        <label className="flex flex-col gap-1.5">
          <span className="text-sm font-medium text-stone-700">Location</span>
          <input
            type="text"
            value={location}
            onChange={(e) => setLocation(e.target.value)}
            className="rounded-lg border border-stone-300 px-3 py-2 text-stone-900 focus:outline-none focus:ring-2 focus:ring-teal-700/40 focus:border-teal-700"
          />
        </label>
        <div className="flex flex-col gap-1.5 justify-end">
          <span className="invisible text-sm font-medium select-none">placeholder</span>
          <label className="flex items-center gap-2 text-stone-700 py-2">
            <input
              type="checkbox"
              checked={isRemote}
              onChange={(e) => setIsRemote(e.target.checked)}
              className="h-4 w-4 rounded border-stone-300 text-teal-700 focus:ring-teal-700/40"
            />
            Remote only
          </label>
        </div>
        <label className="flex flex-col gap-1.5">
          <span className="text-sm font-medium text-stone-700">Results</span>
          <select
            value={resultLimit}
            onChange={(e) => setResultLimit(Number(e.target.value))}
            className="rounded-lg border border-stone-300 px-3 py-2 text-stone-900 focus:outline-none focus:ring-2 focus:ring-teal-700/40 focus:border-teal-700"
          >
            <option value={10}>10 jobs</option>
            <option value={20}>20 jobs</option>
            <option value={30}>30 jobs</option>
          </select>
        </label>
      </div>

      <button
        type="submit"
        className="mt-8 w-full rounded-lg bg-teal-800 py-3 font-medium text-white transition-colors hover:bg-teal-900"
      >
        Find matching jobs
      </button>

      <button
        type="button"
        onClick={onReplace}
        className="mt-3 w-full flex items-center justify-center gap-2 rounded-lg border border-stone-300 py-3 text-sm font-medium text-stone-600 transition-colors hover:border-stone-400 hover:text-stone-800"
      >
        <RotateCcw size={14} />
        Replace resume
      </button>
    </form>
  );
}
