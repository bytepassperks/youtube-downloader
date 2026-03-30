import { useState, useEffect } from "react";
import { useParams, Link } from "react-router-dom";
import { getContentFolder, getDownloadUrl, getMyContent } from "../lib/api";
import {
  ArrowLeft,
  Download,
  Folder,
  File,
  Loader2,
  Lock,
} from "lucide-react";

interface ContentItem {
  id: number;
  job_id: number;
  file_name: string;
  file_path: string;
  file_size: number;
  created_at: string;
}

interface FolderView {
  title: string;
  slug: string;
  files: ContentItem[];
  total_size: number;
  file_count: number;
}

interface ContentEntry {
  id: number;
  title: string;
  download_slug: string;
  created_at: string;
}

function formatSize(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
}

function groupByFolder(files: ContentItem[]): Record<string, ContentItem[]> {
  const groups: Record<string, ContentItem[]> = {};
  for (const file of files) {
    const parts = file.file_path.split("/");
    const folder = parts.length > 1 ? parts.slice(0, -1).join("/") : "Root";
    if (!groups[folder]) groups[folder] = [];
    groups[folder].push(file);
  }
  return groups;
}

function ContentListPage() {
  const [content, setContent] = useState<ContentEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getMyContent()
      .then(setContent)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 text-blue-500 animate-spin" />
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-2xl font-bold text-white mb-6">My Content</h1>
      {content.length === 0 ? (
        <div className="text-center py-16 bg-gray-900 rounded-xl border border-gray-800">
          <Lock className="w-12 h-12 text-gray-600 mx-auto mb-4" />
          <p className="text-gray-400 text-lg">No content available</p>
          <p className="text-gray-500 mt-1">
            Contact your admin to get access to content
          </p>
        </div>
      ) : (
        <div className="grid gap-4">
          {content.map((item) => (
            <Link
              key={item.id}
              to={`/content/${item.download_slug}`}
              className="bg-gray-900 rounded-xl border border-gray-800 p-5 hover:border-blue-600 transition-colors group"
            >
              <div className="flex items-center gap-3">
                <Folder className="w-8 h-8 text-blue-400 group-hover:text-blue-300" />
                <div>
                  <h3 className="text-lg font-semibold text-white group-hover:text-blue-300">
                    {item.title}
                  </h3>
                  <p className="text-gray-500 text-sm">
                    Added {new Date(item.created_at).toLocaleDateString()}
                  </p>
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function ContentFolderPage() {
  const { slug } = useParams<{ slug: string }>();
  const [folder, setFolder] = useState<FolderView | null>(null);
  const [loading, setLoading] = useState(true);
  const [downloading, setDownloading] = useState<number | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!slug) return;
    getContentFolder(slug)
      .then(setFolder)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [slug]);

  const handleDownload = async (fileId: number, fileName: string) => {
    setDownloading(fileId);
    try {
      const data = await getDownloadUrl(fileId);
      const a = document.createElement("a");
      a.href = data.url;
      a.download = fileName;
      a.target = "_blank";
      a.click();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Download failed");
    } finally {
      setDownloading(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 text-blue-500 animate-spin" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-center py-16">
        <Lock className="w-12 h-12 text-red-400 mx-auto mb-4" />
        <p className="text-red-400 text-lg">{error}</p>
        <Link to="/content" className="text-blue-400 hover:text-blue-300 mt-4 inline-block">
          Back to content
        </Link>
      </div>
    );
  }

  if (!folder) return null;

  const grouped = groupByFolder(folder.files);

  return (
    <div>
      <div className="flex items-center gap-3 mb-6">
        <Link to="/content" className="text-gray-400 hover:text-white transition-colors">
          <ArrowLeft className="w-5 h-5" />
        </Link>
        <div>
          <h1 className="text-2xl font-bold text-white">{folder.title}</h1>
          <p className="text-gray-500 text-sm">
            {folder.file_count} files - {formatSize(folder.total_size)}
          </p>
        </div>
      </div>

      <div className="space-y-6">
        {Object.entries(grouped)
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([folderName, files]) => (
            <div key={folderName} className="bg-gray-900 rounded-xl border border-gray-800">
              <div className="flex items-center gap-2 px-5 py-3 border-b border-gray-800">
                <Folder className="w-5 h-5 text-blue-400" />
                <span className="text-white font-medium">{folderName}</span>
                <span className="text-gray-500 text-sm">({files.length} files)</span>
              </div>
              <div className="divide-y divide-gray-800/50">
                {files.map((file) => (
                  <div
                    key={file.id}
                    className="flex items-center justify-between px-5 py-3 hover:bg-gray-800/30"
                  >
                    <div className="flex items-center gap-3 min-w-0 flex-1">
                      <File className="w-4 h-4 text-gray-500 flex-shrink-0" />
                      <span className="text-gray-300 truncate">{file.file_name}</span>
                      <span className="text-gray-600 text-sm flex-shrink-0">
                        {formatSize(file.file_size)}
                      </span>
                    </div>
                    <button
                      onClick={() => handleDownload(file.id, file.file_name)}
                      disabled={downloading === file.id}
                      className="flex items-center gap-1 text-blue-400 hover:text-blue-300 px-3 py-1 rounded-lg hover:bg-blue-600/10 transition-colors flex-shrink-0"
                    >
                      {downloading === file.id ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                      ) : (
                        <Download className="w-4 h-4" />
                      )}
                      <span className="text-sm">Download</span>
                    </button>
                  </div>
                ))}
              </div>
            </div>
          ))}
      </div>
    </div>
  );
}

export default function ContentPage() {
  const { slug } = useParams<{ slug: string }>();
  if (slug) return <ContentFolderPage />;
  return <ContentListPage />;
}
