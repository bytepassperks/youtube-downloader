import { useState, useEffect, useCallback } from "react";
import { listJobs, startJob, deleteJob, retryJob } from "../lib/api";
import { Link } from "react-router-dom";
import {
  Plus,
  Play,
  Trash2,
  RefreshCw,
  Loader2,
  CheckCircle2,
  XCircle,
  Clock,
  ArrowUpFromLine,
  ArrowDownToLine,
  Users,
} from "lucide-react";

interface Job {
  id: number;
  title: string;
  mega_link: string;
  excluded_files: string[];
  storage_target: string;
  status: string;
  progress: number;
  total_files: number;
  uploaded_files: number;
  download_slug: string;
  error_message: string;
  created_at: string;
  completed_at: string | null;
  telegram_sent: boolean;
}

export default function DashboardPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchJobs = useCallback(async () => {
    try {
      const data = await listJobs();
      setJobs(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchJobs();
    const interval = setInterval(fetchJobs, 5000);
    return () => clearInterval(interval);
  }, [fetchJobs]);

  const handleStart = async (id: number) => {
    try {
      await startJob(id);
      fetchJobs();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Failed to start job");
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm("Delete this job?")) return;
    try {
      await deleteJob(id);
      fetchJobs();
    } catch (err) {
      console.error(err);
    }
  };

  const handleRetry = async (id: number) => {
    try {
      await retryJob(id);
      fetchJobs();
    } catch (err) {
      console.error(err);
    }
  };

  const statusIcon = (status: string) => {
    switch (status) {
      case "completed":
        return <CheckCircle2 className="w-5 h-5 text-green-400" />;
      case "failed":
        return <XCircle className="w-5 h-5 text-red-400" />;
      case "downloading":
        return <ArrowDownToLine className="w-5 h-5 text-blue-400 animate-pulse" />;
      case "uploading":
        return <ArrowUpFromLine className="w-5 h-5 text-purple-400 animate-pulse" />;
      default:
        return <Clock className="w-5 h-5 text-yellow-400" />;
    }
  };

  const statusColor = (status: string) => {
    switch (status) {
      case "completed":
        return "text-green-400";
      case "failed":
        return "text-red-400";
      case "downloading":
        return "text-blue-400";
      case "uploading":
        return "text-purple-400";
      default:
        return "text-yellow-400";
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 text-blue-500 animate-spin" />
      </div>
    );
  }

  return (
    <div>
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold text-white">Transfer Jobs</h1>
        <div className="flex gap-3">
          <Link
            to="/members"
            className="flex items-center gap-2 bg-gray-700 hover:bg-gray-600 text-white px-4 py-2 rounded-lg transition-colors"
          >
            <Users className="w-4 h-4" />
            Members
          </Link>
          <Link
            to="/new-job"
            className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg transition-colors"
          >
            <Plus className="w-4 h-4" />
            New Transfer
          </Link>
        </div>
      </div>

      {jobs.length === 0 ? (
        <div className="text-center py-16 bg-gray-900 rounded-xl border border-gray-800">
          <ArrowUpFromLine className="w-12 h-12 text-gray-600 mx-auto mb-4" />
          <p className="text-gray-400 text-lg">No transfers yet</p>
          <p className="text-gray-500 mt-1">Create your first transfer to get started</p>
        </div>
      ) : (
        <div className="space-y-4">
          {jobs.map((job) => (
            <div
              key={job.id}
              className="bg-gray-900 rounded-xl border border-gray-800 p-5 hover:border-gray-700 transition-colors"
            >
              <div className="flex justify-between items-start">
                <div className="flex-1">
                  <div className="flex items-center gap-3">
                    {statusIcon(job.status)}
                    <h3 className="text-lg font-semibold text-white">{job.title}</h3>
                    <span
                      className={`text-xs font-medium px-2 py-1 rounded-full ${
                        job.storage_target === "idrive"
                          ? "bg-emerald-900/50 text-emerald-400"
                          : "bg-orange-900/50 text-orange-400"
                      }`}
                    >
                      {job.storage_target === "idrive" ? "iDrive" : "Backblaze B2"}
                    </span>
                    {job.telegram_sent && (
                      <span className="text-xs font-medium px-2 py-1 rounded-full bg-blue-900/50 text-blue-400">
                        Telegram sent
                      </span>
                    )}
                  </div>
                  <p className="text-gray-500 text-sm mt-1 truncate max-w-lg">
                    {job.mega_link}
                  </p>
                  {job.excluded_files.length > 0 && (
                    <p className="text-gray-600 text-xs mt-1">
                      Excluded: {job.excluded_files.join(", ")}
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-2 ml-4">
                  {job.status === "queued" && (
                    <button
                      onClick={() => handleStart(job.id)}
                      className="p-2 bg-green-600 hover:bg-green-700 rounded-lg text-white transition-colors"
                      title="Start transfer"
                    >
                      <Play className="w-4 h-4" />
                    </button>
                  )}
                  {job.status === "failed" && (
                    <button
                      onClick={() => handleRetry(job.id)}
                      className="p-2 bg-yellow-600 hover:bg-yellow-700 rounded-lg text-white transition-colors"
                      title="Retry"
                    >
                      <RefreshCw className="w-4 h-4" />
                    </button>
                  )}
                  <button
                    onClick={() => handleDelete(job.id)}
                    className="p-2 bg-red-600/20 hover:bg-red-600/40 rounded-lg text-red-400 transition-colors"
                    title="Delete"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>

              {/* Progress bar */}
              {(job.status === "downloading" || job.status === "uploading") && (
                <div className="mt-4">
                  <div className="flex justify-between text-sm mb-1">
                    <span className={statusColor(job.status)}>
                      {job.status === "downloading" ? "Downloading..." : "Uploading..."}
                    </span>
                    <span className="text-gray-400">
                      {job.uploaded_files}/{job.total_files} files ({job.progress}%)
                    </span>
                  </div>
                  <div className="w-full bg-gray-800 rounded-full h-2">
                    <div
                      className={`h-2 rounded-full transition-all ${
                        job.status === "downloading" ? "bg-blue-500" : "bg-purple-500"
                      }`}
                      style={{ width: `${job.progress}%` }}
                    />
                  </div>
                </div>
              )}

              {/* Error message */}
              {job.status === "failed" && job.error_message && (
                <div className="mt-3 bg-red-900/20 border border-red-800 rounded-lg px-3 py-2">
                  <p className="text-red-400 text-sm">{job.error_message}</p>
                </div>
              )}

              {/* Completed info */}
              {job.status === "completed" && (
                <div className="mt-3 flex items-center gap-4 text-sm text-gray-400">
                  <span>{job.total_files} files uploaded</span>
                  <span>Slug: {job.download_slug}</span>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
