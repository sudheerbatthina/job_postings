import { useState, useCallback } from "react";
import { useDropzone } from "react-dropzone";
import { FileText, Upload, X } from "lucide-react";

export default function UploadForm({ onSubmit }) {
  const [file, setFile] = useState(null);
  const [location, setLocation] = useState("United States");
  const [isRemote, setIsRemote] = useState(false);
  const [resultLimit, setResultLimit] = useState(10);

  const onDrop = useCallback((accepted) => {
    if (accepted[0]) setFile(accepted[0]);
  }, []);

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    maxFiles: 1,
    accept: {
      "application/pdf": [".pdf"],
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
      "text/plain": [".txt"],
    },
  });

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!file) return;
    onSubmit({ file, location, isRemote, resultLimit });
  };

  return (
    <form onSubmit={handleSubmit} className="w-full max-w-xl mx-auto">
      <h1 className="text-2xl font-semibold tracking-tight text-stone-900">
        Find roles that match your resume
      </h1>
      <p className="mt-2 text-stone-600">
        Upload your resume. We'll extract your skills, scrape recent job postings across
        LinkedIn, Indeed, Google Jobs, Glassdoor, and company ATS sources, then score and
        rank each match using AI.
      </p>
      <p className="mt-1 text-sm text-stone-400">
        Searches recent postings from the last 30 hours.
      </p>

      <div
        {...getRootProps()}
        className={`mt-8 flex flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed px-6 py-12 text-center transition-colors cursor-pointer
          ${isDragActive ? "border-teal-700 bg-teal-50" : "border-stone-300 bg-white hover:border-stone-400"}`}
      >
        <input {...getInputProps()} />
        {file ? (
          <div className="flex items-center gap-3 text-stone-800">
            <FileText size={20} className="text-teal-700" />
            <span className="font-medium">{file.name}</span>
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); setFile(null); }}
              className="text-stone-400 hover:text-stone-700"
              aria-label="Remove file"
            >
              <X size={16} />
            </button>
          </div>
        ) : (
          <>
            <Upload size={28} className="text-stone-400" />
            <p className="text-stone-600">
              <span className="font-medium text-teal-700">Choose a file</span> or drag it here
            </p>
            <p className="text-sm text-stone-400">PDF, DOCX, or TXT</p>
          </>
        )}
      </div>

      <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1.5">
          <span className="text-sm font-medium text-stone-700">Location</span>
          <input
            type="text"
            value={location}
            onChange={(e) => setLocation(e.target.value)}
            className="rounded-lg border border-stone-300 px-3 py-2 text-stone-900 focus:outline-none focus:ring-2 focus:ring-teal-700/40 focus:border-teal-700"
          />
        </label>
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

      <label className="mt-4 flex items-center gap-2 text-stone-700">
        <input
          type="checkbox"
          checked={isRemote}
          onChange={(e) => setIsRemote(e.target.checked)}
          className="h-4 w-4 rounded border-stone-300 text-teal-700 focus:ring-teal-700/40"
        />
        Remote only
      </label>

      <button
        type="submit"
        disabled={!file}
        className="mt-8 w-full rounded-lg bg-teal-800 py-3 font-medium text-white transition-colors hover:bg-teal-900 disabled:bg-stone-300 disabled:cursor-not-allowed"
      >
        Find matching jobs
      </button>
    </form>
  );
}
