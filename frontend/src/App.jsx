import { useEffect, useRef, useState } from "react";
import UploadForm from "./components/UploadForm";
import ProgressView from "./components/ProgressView";
import ResultsTable from "./components/ResultsTable";
import { submitResume, getJobStatus } from "./api";

const POLL_INTERVAL_MS = 2000;

export default function App() {
  const [stage, setStage] = useState("upload"); // upload | polling | done | error
  const [message, setMessage] = useState("");
  const [results, setResults] = useState([]);
  const [jobId, setJobId] = useState(null);
  const pollRef = useRef(null);

  useEffect(() => () => clearInterval(pollRef.current), []);

  const handleSubmit = async (params) => {
    setStage("polling");
    setMessage("Uploading resume...");
    try {
      const { job_id } = await submitResume(params);
      setJobId(job_id);
      pollRef.current = setInterval(() => poll(job_id), POLL_INTERVAL_MS);
    } catch (err) {
      setStage("error");
      setMessage(err.message);
    }
  };

  const poll = async (id) => {
    try {
      const data = await getJobStatus(id);
      setMessage(data.message);
      if (data.status === "done") {
        clearInterval(pollRef.current);
        setResults(data.results || []);
        setStage("done");
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
    setStage("upload");
    setResults([]);
    setJobId(null);
    setMessage("");
  };

  return (
    <div className="min-h-screen px-4 py-16">
      {stage === "upload" && <UploadForm onSubmit={handleSubmit} />}
      {stage === "polling" && <ProgressView message={message} />}
      {stage === "done" && <ResultsTable results={results} jobId={jobId} onReset={reset} />}
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
