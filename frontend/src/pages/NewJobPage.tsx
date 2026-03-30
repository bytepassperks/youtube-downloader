import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { createJob, startJob } from "../lib/api";
import { ArrowLeft, Upload } from "lucide-react";
import { Link } from "react-router-dom";

export default function NewJobPage() {
  const [title, setTitle] = useState("");
  const [megaLink, setMegaLink] = useState("");
  const [excludedFiles, setExcludedFiles] = useState("");
  const [storageTarget, setStorageTarget] = useState("idrive");
  const [autoStart, setAutoStart] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const excluded = excludedFiles
        .split("\n")
        .map((f) => f.trim())
        .filter((f) => f.length > 0);

      const job = await createJob({
        title,
        mega_link: megaLink,
        exclude_files: excluded,
        storage_target: storageTarget,
      });

      if (autoStart) {
        await startJob(job.id);
      }

      navigate("/");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to create job");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-2xl mx-auto">
      <div className="flex items-center gap-3 mb-6">
        <Link to="/" className="text-gray-400 hover:text-white transition-colors">
          <ArrowLeft className="w-5 h-5" />
        </Link>
        <h1 className="text-2xl font-bold text-white">New Transfer</h1>
      </div>

      <form
        onSubmit={handleSubmit}
        className="bg-gray-900 rounded-xl p-8 border border-gray-800"
      >
        {error && (
          <div className="bg-red-900/50 border border-red-700 text-red-300 px-4 py-3 rounded-lg mb-6">
            {error}
          </div>
        )}

        <div className="mb-5">
          <label className="block text-gray-300 text-sm font-medium mb-2">
            Title *
          </label>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
            placeholder="e.g., Faisal Khan – AI Creator Academy"
            required
          />
          <p className="text-gray-500 text-xs mt-1">
            This title will be posted to Telegram along with the download link
          </p>
        </div>

        <div className="mb-5">
          <label className="block text-gray-300 text-sm font-medium mb-2">
            Mega.nz Link *
          </label>
          <input
            type="url"
            value={megaLink}
            onChange={(e) => setMegaLink(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
            placeholder="https://mega.nz/folder/..."
            required
          />
        </div>

        <div className="mb-5">
          <label className="block text-gray-300 text-sm font-medium mb-2">
            Files to Exclude
          </label>
          <textarea
            value={excludedFiles}
            onChange={(e) => setExcludedFiles(e.target.value)}
            rows={4}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
            placeholder="One filename per line, e.g.:&#10;Upgrade Your Account VIP - edollarearn.com.txt&#10;README.txt"
          />
          <p className="text-gray-500 text-xs mt-1">
            Files matching these names (case-insensitive) will be excluded
          </p>
        </div>

        <div className="mb-5">
          <label className="block text-gray-300 text-sm font-medium mb-2">
            Storage Target
          </label>
          <div className="flex gap-4">
            <label
              className={`flex-1 cursor-pointer rounded-lg border p-4 transition-colors ${
                storageTarget === "idrive"
                  ? "border-emerald-500 bg-emerald-900/20"
                  : "border-gray-700 bg-gray-800 hover:border-gray-600"
              }`}
            >
              <input
                type="radio"
                name="storage"
                value="idrive"
                checked={storageTarget === "idrive"}
                onChange={(e) => setStorageTarget(e.target.value)}
                className="hidden"
              />
              <div className="text-white font-medium">iDrive E2</div>
              <div className="text-gray-400 text-sm">Primary (100TB)</div>
            </label>
            <label
              className={`flex-1 cursor-pointer rounded-lg border p-4 transition-colors ${
                storageTarget === "b2"
                  ? "border-orange-500 bg-orange-900/20"
                  : "border-gray-700 bg-gray-800 hover:border-gray-600"
              }`}
            >
              <input
                type="radio"
                name="storage"
                value="b2"
                checked={storageTarget === "b2"}
                onChange={(e) => setStorageTarget(e.target.value)}
                className="hidden"
              />
              <div className="text-white font-medium">Backblaze B2</div>
              <div className="text-gray-400 text-sm">Overflow</div>
            </label>
          </div>
        </div>

        <div className="mb-6">
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={autoStart}
              onChange={(e) => setAutoStart(e.target.checked)}
              className="w-5 h-5 rounded bg-gray-800 border-gray-700 text-blue-600 focus:ring-blue-500"
            />
            <span className="text-gray-300">Start transfer immediately</span>
          </label>
        </div>

        <button
          type="submit"
          disabled={loading}
          className="w-full flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-800 disabled:cursor-not-allowed text-white font-medium py-3 rounded-lg transition-colors"
        >
          {loading ? (
            <>Processing...</>
          ) : (
            <>
              <Upload className="w-5 h-5" />
              Create Transfer
            </>
          )}
        </button>
      </form>
    </div>
  );
}
