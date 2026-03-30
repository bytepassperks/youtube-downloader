import { useState, useEffect } from "react";
import { getSettings, updateSettings, reshareAllTelegram } from "../lib/api";
import { Save, RefreshCw, Send, Eye, EyeOff, Loader2 } from "lucide-react";

interface SettingsData {
  telegram_bot_token: string;
  telegram_group_chat_id: string;
  idrive_access_key: string;
  idrive_secret_key: string;
  idrive_endpoint: string;
  idrive_bucket: string;
  idrive_region: string;
  b2_key_id: string;
  b2_app_key: string;
  b2_bucket_name: string;
  portal_base_url: string;
  admin_email: string;
  admin_password: string;
}

const FIELD_GROUPS = [
  {
    title: "Telegram",
    fields: [
      { key: "telegram_bot_token", label: "Bot Token", sensitive: true },
      { key: "telegram_group_chat_id", label: "Group Chat ID", sensitive: false },
    ],
  },
  {
    title: "iDrive E2 (S3)",
    fields: [
      { key: "idrive_access_key", label: "Access Key ID", sensitive: true },
      { key: "idrive_secret_key", label: "Secret Access Key", sensitive: true },
      { key: "idrive_endpoint", label: "Endpoint", sensitive: false },
      { key: "idrive_bucket", label: "Bucket Name", sensitive: false },
      { key: "idrive_region", label: "Region", sensitive: false },
    ],
  },
  {
    title: "Backblaze B2",
    fields: [
      { key: "b2_key_id", label: "Key ID", sensitive: true },
      { key: "b2_app_key", label: "App Key", sensitive: true },
      { key: "b2_bucket_name", label: "Bucket Name", sensitive: false },
    ],
  },
  {
    title: "Portal & Admin",
    fields: [
      { key: "portal_base_url", label: "Portal Base URL", sensitive: false },
      { key: "admin_email", label: "Admin Email", sensitive: false },
      { key: "admin_password", label: "Admin Password", sensitive: true },
    ],
  },
];

export default function SettingsPage() {
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [resharing, setResharing] = useState(false);
  const [message, setMessage] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [showSensitive, setShowSensitive] = useState<Record<string, boolean>>({});

  useEffect(() => {
    loadSettings();
  }, []);

  async function loadSettings() {
    try {
      const data = await getSettings();
      setSettings(data);
      // Don't populate form with masked values - only set non-sensitive fields
      const initial: Record<string, string> = {};
      FIELD_GROUPS.forEach((g) =>
        g.fields.forEach((f) => {
          const val = data[f.key as keyof SettingsData] || "";
          // If value contains asterisks, it's masked - leave form empty for that field
          if (val.includes("****")) {
            initial[f.key] = "";
          } else {
            initial[f.key] = val;
          }
        })
      );
      setForm(initial);
    } catch {
      setMessage({ type: "error", text: "Failed to load settings" });
    } finally {
      setLoading(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setMessage(null);
    try {
      // Only send fields that have been changed (non-empty)
      const changed: Record<string, string> = {};
      Object.entries(form).forEach(([key, value]) => {
        if (value && value.trim()) {
          changed[key] = value.trim();
        }
      });
      if (Object.keys(changed).length === 0) {
        setMessage({ type: "error", text: "No changes to save" });
        setSaving(false);
        return;
      }
      await updateSettings(changed);
      setMessage({ type: "success", text: "Settings saved successfully" });
      // Reload to get fresh masked values
      await loadSettings();
    } catch {
      setMessage({ type: "error", text: "Failed to save settings" });
    } finally {
      setSaving(false);
    }
  }

  async function handleReshare() {
    setResharing(true);
    setMessage(null);
    try {
      const result = await reshareAllTelegram();
      setMessage({ type: "success", text: result.message });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Failed to reshare";
      setMessage({ type: "error", text: msg });
    } finally {
      setResharing(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="w-8 h-8 text-blue-500 animate-spin" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Settings</h1>
        <div className="flex gap-3">
          <button
            onClick={handleReshare}
            disabled={resharing}
            className="flex items-center gap-2 px-4 py-2 bg-purple-600 hover:bg-purple-700 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
          >
            {resharing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            Re-share All to Telegram
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            Save Changes
          </button>
        </div>
      </div>

      {message && (
        <div
          className={`p-3 rounded-lg text-sm ${
            message.type === "success"
              ? "bg-green-900/50 text-green-400 border border-green-800"
              : "bg-red-900/50 text-red-400 border border-red-800"
          }`}
        >
          {message.text}
        </div>
      )}

      <p className="text-gray-400 text-sm">
        Configure your service credentials below. Sensitive fields are masked. Leave a field empty to keep the current value.
      </p>

      <div className="grid gap-6">
        {FIELD_GROUPS.map((group) => (
          <div key={group.title} className="bg-gray-900 border border-gray-800 rounded-xl p-6">
            <h2 className="text-lg font-semibold text-white mb-4">{group.title}</h2>
            <div className="grid gap-4 sm:grid-cols-2">
              {group.fields.map((field) => {
                const masked = settings?.[field.key as keyof SettingsData] || "";
                const isVisible = showSensitive[field.key];
                return (
                  <div key={field.key}>
                    <label className="block text-sm font-medium text-gray-400 mb-1">
                      {field.label}
                    </label>
                    <div className="relative">
                      <input
                        type={field.sensitive && !isVisible ? "password" : "text"}
                        value={form[field.key] || ""}
                        onChange={(e) => setForm({ ...form, [field.key]: e.target.value })}
                        placeholder={field.sensitive ? masked : "Not set"}
                        className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-white text-sm placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent pr-10"
                      />
                      {field.sensitive && (
                        <button
                          type="button"
                          onClick={() =>
                            setShowSensitive({ ...showSensitive, [field.key]: !isVisible })
                          }
                          className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
                        >
                          {isVisible ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                        </button>
                      )}
                    </div>
                    {field.sensitive && masked && (
                      <p className="text-xs text-gray-600 mt-1">Current: {masked}</p>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h2 className="text-lg font-semibold text-white mb-2">Telegram Re-share</h2>
        <p className="text-gray-400 text-sm mb-4">
          If your Telegram account was banned or you switched to a new bot/group, update the
          Telegram credentials above, save, then click the button below to re-send all completed
          job notifications to your new group.
        </p>
        <button
          onClick={handleReshare}
          disabled={resharing}
          className="flex items-center gap-2 px-4 py-2 bg-purple-600 hover:bg-purple-700 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
        >
          {resharing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          Re-share All Completed Jobs to Telegram
        </button>
      </div>
    </div>
  );
}
