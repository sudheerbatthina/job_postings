import { Loader2 } from "lucide-react";

export default function ProgressView({ message }) {
  return (
    <div className="w-full max-w-xl mx-auto flex flex-col items-center gap-4 py-16 text-center">
      <Loader2 size={28} className="animate-spin text-teal-700" />
      <p className="text-stone-700">{message || "Working on it..."}</p>
      <p className="text-sm text-stone-400">
        Live scraping can take a minute or two — LinkedIn and Indeed rate-limit aggressive requests,
        so we go slow on purpose.
      </p>
    </div>
  );
}
