import { Loader2 } from "lucide-react";

export default function ProgressView({ message }) {
  const parts = message ? message.split(" → ") : null;
  const isTrail = parts && parts.length > 1;

  return (
    <div className="w-full max-w-xl mx-auto flex flex-col items-center gap-4 py-16 text-center">
      <Loader2 size={28} className="animate-spin text-teal-700" />
      {isTrail ? (
        <ul className="w-full text-left text-sm text-stone-700 space-y-1 font-mono">
          {parts.map((part, i) => (
            <li key={i} className="flex gap-2">
              <span className="text-stone-400 shrink-0">{i === 0 ? "  " : "→"}</span>
              <span>{part}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-stone-700">{message || "Working on it..."}</p>
      )}
      <p className="text-sm text-stone-400">
        Live scraping can take a minute or two — LinkedIn and Indeed rate-limit aggressive requests,
        so we go slow on purpose.
      </p>
    </div>
  );
}
