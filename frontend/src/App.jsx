import { useEffect, useRef, useState } from "react";
import UploadForm from "./components/UploadForm";
import ReadyToSearch from "./components/ReadyToSearch";
import ProgressView from "./components/ProgressView";
import ResultsTable from "./components/ResultsTable";
import { submitResume, getJobStatus, getStoredResume } from "./api";

const POLL_INTERVAL_MS = 2000;
const MAX_POLLS = 90; // 3 minutes at 2 s interval

export default function App() {
  // upload | ready | polling | done | error
  const [stage, setStage] = useState("upload");
  const [message, setMessage] = useState("");
  const [results, setResults] = useState([]);
  const [lowConfidenceResults, setLowConfidenceResults] = useState([]);
  const [jobId, setJobId] = useState(null);
  const [storedResume, setStoredResume] = useState(null);
  const pollRef = useRef(null);
  const pollCountRef = useRef(0);

  useEffect(() => {
    getStoredResume()
      .then((data) => {
        if (data.filename) {
          setStoredResume(data);
          setStage("ready");
        }
      })
      .catch(() => {});
    return () => clearInterval(pollRef.current);
  }, []);

  const handleSubmit = async (params) => {
    setStage("polling");
    setMessage(params.file ? "Uploading resume..." : "Starting search...");
    pollCountRef.current = 0;
    try {
      const { job_id } = await submitResume(params);
      setJobId(job_id);
      pollRef.current = setInterval(() => poll(job_id), POLL_INTERVAL_MS);
    } catch (err) {
      setStage("error");
      if (err.status === 429) {
        setMessage("You've hit today's search limit (4/day) — try again tomorrow.");
      } else {
        setMessage(err.message);
      }
    }
  };

  const poll = async (id) => {
    pollCountRef.current += 1;
    if (pollCountRef.current > MAX_POLLS) {
      clearInterval(pollRef.current);
      setStage("error");
      setMessage(
        "This is taking longer than expected — the job boards may be rate limiting us. Try again in a few minutes."
      );
      return;
    }
    try {
      const data = await getJobStatus(id);
      setMessage(data.message);
      if (data.status === "done") {
        clearInterval(pollRef.current);
        setResults(data.results || []);
        setLowConfidenceResults(data.low_confidence_results || []);
        setStage("done");
        // Refresh stored resume info so "New search" goes back to ready stage
        getStoredResume()
          .then((d) => { if (d.filename) setStoredResume(d); })
          .catch(() => {});
      } else if (data.status === "error") {
        clearInterval(pollRef.current);
        setStage("error");
        setMessage(data.error || "Something went wrong");
      }
    } catch (err) {
      clearInterval(pollRef.current);
      setStage("error");
      setMessage(err.message);
    }
  };

  const reset = () => {
    setResults([]);
    setLowConfidenceResults([]);
    setJobId(null);
    setMessage("");
    setStage(storedResume ? "ready" : "upload");
  };

  const replaceResume = () => {
    setStoredResume(null);
    setStage("upload");
  };

  return (
    <div className="min-h-screen px-4 py-16">
      {stage === "upload" && <UploadForm onSubmit={handleSubmit} />}
      {stage === "ready" && (
        <ReadyToSearch
          storedResume={storedResume}
          onSubmit={handleSubmit}
          onReplace={replaceResume}
        />
      )}
      {stage === "polling" && <ProgressView message={message} />}
      {stage === "done" && (
        <ResultsTable
          results={results}
          lowConfidenceResults={lowConfidenceResults}
          jobId={jobId}
          onReset={reset}
        />
      )}
      {stage === "error" && (
        <div className="w-full max-w-xl mx-auto text-center py-16">
          <p className="font-medium text-stone-900">{message}</p>
          <button onClick={reset} className="mt-4 text-teal-700 font-medium hover:underline">
            Try again
          </button>
        </div>
      )}
    </div>
  );
}
