import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import {
  listMembers,
  createMember,
  deleteMember,
  updateMember,
} from "../lib/api";
import {
  ArrowLeft,
  Plus,
  Trash2,
  UserCheck,
  UserX,
  Loader2,
} from "lucide-react";

interface Member {
  id: number;
  email: string;
  is_active: boolean;
  created_at: string;
  max_downloads_per_day: number;
}

export default function MembersPage() {
  const [members, setMembers] = useState<Member[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [error, setError] = useState("");

  const fetchMembers = async () => {
    try {
      const data = await listMembers();
      setMembers(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMembers();
  }, []);

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      await createMember(newEmail, newPassword);
      setNewEmail("");
      setNewPassword("");
      setShowAdd(false);
      fetchMembers();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to add member");
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm("Remove this member?")) return;
    try {
      await deleteMember(id);
      fetchMembers();
    } catch (err) {
      console.error(err);
    }
  };

  const handleToggleActive = async (member: Member) => {
    try {
      await updateMember(member.id, { is_active: !member.is_active });
      fetchMembers();
    } catch (err) {
      console.error(err);
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
        <div className="flex items-center gap-3">
          <Link to="/" className="text-gray-400 hover:text-white transition-colors">
            <ArrowLeft className="w-5 h-5" />
          </Link>
          <h1 className="text-2xl font-bold text-white">Members</h1>
          <span className="text-gray-500 text-sm">({members.length})</span>
        </div>
        <button
          onClick={() => setShowAdd(!showAdd)}
          className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg transition-colors"
        >
          <Plus className="w-4 h-4" />
          Add Member
        </button>
      </div>

      {showAdd && (
        <form
          onSubmit={handleAdd}
          className="bg-gray-900 rounded-xl p-6 border border-gray-800 mb-6"
        >
          {error && (
            <div className="bg-red-900/50 border border-red-700 text-red-300 px-4 py-3 rounded-lg mb-4">
              {error}
            </div>
          )}
          <div className="flex gap-4">
            <input
              type="email"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
              placeholder="member@example.com"
              className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
              required
            />
            <input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder="Password"
              className="w-48 bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
              required
            />
            <button
              type="submit"
              className="bg-green-600 hover:bg-green-700 text-white px-6 py-2 rounded-lg transition-colors"
            >
              Add
            </button>
          </div>
        </form>
      )}

      {members.length === 0 ? (
        <div className="text-center py-16 bg-gray-900 rounded-xl border border-gray-800">
          <p className="text-gray-400 text-lg">No members yet</p>
          <p className="text-gray-500 mt-1">Add members to give them download access</p>
        </div>
      ) : (
        <div className="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="border-b border-gray-800">
                <th className="text-left text-gray-400 text-sm font-medium px-6 py-3">Email</th>
                <th className="text-left text-gray-400 text-sm font-medium px-6 py-3">Status</th>
                <th className="text-left text-gray-400 text-sm font-medium px-6 py-3">Daily Limit</th>
                <th className="text-left text-gray-400 text-sm font-medium px-6 py-3">Joined</th>
                <th className="text-right text-gray-400 text-sm font-medium px-6 py-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {members.map((member) => (
                <tr key={member.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                  <td className="px-6 py-4 text-white">{member.email}</td>
                  <td className="px-6 py-4">
                    <span
                      className={`inline-flex items-center gap-1 text-sm ${
                        member.is_active ? "text-green-400" : "text-red-400"
                      }`}
                    >
                      {member.is_active ? (
                        <UserCheck className="w-4 h-4" />
                      ) : (
                        <UserX className="w-4 h-4" />
                      )}
                      {member.is_active ? "Active" : "Disabled"}
                    </span>
                  </td>
                  <td className="px-6 py-4 text-gray-400">{member.max_downloads_per_day}/day</td>
                  <td className="px-6 py-4 text-gray-500 text-sm">
                    {new Date(member.created_at).toLocaleDateString()}
                  </td>
                  <td className="px-6 py-4 text-right">
                    <div className="flex items-center justify-end gap-2">
                      <button
                        onClick={() => handleToggleActive(member)}
                        className={`p-2 rounded-lg transition-colors ${
                          member.is_active
                            ? "bg-yellow-600/20 text-yellow-400 hover:bg-yellow-600/40"
                            : "bg-green-600/20 text-green-400 hover:bg-green-600/40"
                        }`}
                        title={member.is_active ? "Disable" : "Enable"}
                      >
                        {member.is_active ? <UserX className="w-4 h-4" /> : <UserCheck className="w-4 h-4" />}
                      </button>
                      <button
                        onClick={() => handleDelete(member.id)}
                        className="p-2 bg-red-600/20 text-red-400 hover:bg-red-600/40 rounded-lg transition-colors"
                        title="Remove"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
