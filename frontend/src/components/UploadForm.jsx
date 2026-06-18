import { useState, useCallback } from "react";
import { useDropzone } from "react-dropzone";
import { FileText, Upload, X } from "lucide-react";

export default function UploadForm({ onSubmit }) {
  const [file, setFile] = useState(null);
  const [location, setLocation] = useState("United States");
  const [isRemote, setIsRemote] = useState(false);
  const [hoursOld, setHoursOld] = useState(168);

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
    onSubmit({ file, location, isRemote, hoursOld });
  };

  return (
    <form onSubmit={handleSubmit} className="w-full max-w-xl mx-auto">
      <h1 className="text-2xl font-semibold tracking-tight text-stone-900">
        Find AI engineer roles that match your resume
      </h1>
      <p className="mt-2 text-stone-600">
        Upload your resume. We'll scrape recent AI engineer postings across LinkedIn, Indeed,
        Google and ZipRecruiter, score each one against your resume, and rank the matches.
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

      <div className="mt-6 grid grid-cols-2 gap-4">
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
          <span className="text-sm font-medium text-stone-700">Posted within</span>
          <select
            value={hoursOld}
            onChange={(e) => setHoursOld(Number(e.target.value))}
            className="rounded-lg border border-stone-300 px-3 py-2 text-stone-900 focus:outline-none focus:ring-2 focus:ring-teal-700/40 focus:border-teal-700"
          >
            <option value={24}>Last 24 hours</option>
            <option value={72}>Last 3 days</option>
            <option value={168}>Last 7 days</option>
            <option value={336}>Last 14 days</option>
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
