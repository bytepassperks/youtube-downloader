import { useState, useEffect } from "react";
import { Routes, Route, Navigate, Link, useLocation } from "react-router-dom";
import { getMe, logout } from "./lib/api";
import LoginPage from "./pages/LoginPage";
import DashboardPage from "./pages/DashboardPage";
import NewJobPage from "./pages/NewJobPage";
import MembersPage from "./pages/MembersPage";
import ContentPage from "./pages/ContentPage";
import { LayoutDashboard, FolderOpen, LogOut, Loader2 } from "lucide-react";

interface User {
  id: number;
  email: string;
  is_admin: boolean;
}

function Layout({ user, children }: { user: User; children: React.ReactNode }) {
  const location = useLocation();

  const navItems = user.is_admin
    ? [
        { path: "/", label: "Dashboard", icon: LayoutDashboard },
        { path: "/content", label: "Content", icon: FolderOpen },
      ]
    : [{ path: "/content", label: "My Content", icon: FolderOpen }];

  return (
    <div className="min-h-screen bg-gray-950">
      <nav className="bg-gray-900 border-b border-gray-800">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-8">
              <Link to="/" className="text-xl font-bold text-white">
                MegaTransfer
              </Link>
              <div className="flex gap-1">
                {navItems.map((item) => {
                  const Icon = item.icon;
                  const active = location.pathname === item.path;
                  return (
                    <Link
                      key={item.path}
                      to={item.path}
                      className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                        active
                          ? "bg-gray-800 text-white"
                          : "text-gray-400 hover:text-white hover:bg-gray-800/50"
                      }`}
                    >
                      <Icon className="w-4 h-4" />
                      {item.label}
                    </Link>
                  );
                })}
              </div>
            </div>
            <div className="flex items-center gap-4">
              <span className="text-gray-400 text-sm">{user.email}</span>
              {user.is_admin && (
                <span className="text-xs bg-blue-600/20 text-blue-400 px-2 py-1 rounded-full">
                  Admin
                </span>
              )}
              <button
                onClick={logout}
                className="text-gray-400 hover:text-white p-2 rounded-lg hover:bg-gray-800 transition-colors"
                title="Sign out"
              >
                <LogOut className="w-4 h-4" />
              </button>
            </div>
          </div>
        </div>
      </nav>
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {children}
      </main>
    </div>
  );
}

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const token = localStorage.getItem("token");
    if (!token) {
      setLoading(false);
      return;
    }
    getMe()
      .then(setUser)
      .catch(() => localStorage.removeItem("token"))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="min-h-screen bg-gray-950 flex items-center justify-center">
        <Loader2 className="w-8 h-8 text-blue-500 animate-spin" />
      </div>
    );
  }

  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  if (user.is_admin) {
    return (
      <Layout user={user}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/new-job" element={<NewJobPage />} />
          <Route path="/members" element={<MembersPage />} />
          <Route path="/content/:slug" element={<ContentPage />} />
          <Route path="/content" element={<ContentPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    );
  }

  return (
    <Layout user={user}>
      <Routes>
        <Route path="/content/:slug" element={<ContentPage />} />
        <Route path="/content" element={<ContentPage />} />
        <Route path="*" element={<Navigate to="/content" replace />} />
      </Routes>
    </Layout>
  );
}

export default App;
