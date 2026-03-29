const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

function getToken(): string | null {
  return localStorage.getItem("token");
}

async function request(path: string, options: RequestInit = {}): Promise<Response> {
  const token = getToken();
  const headers: Record<string, string> = {
    ...(options.headers as Record<string, string>),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  if (!(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  const res = await fetch(`${API_URL}${path}`, { ...options, headers });
  if (res.status === 401) {
    localStorage.removeItem("token");
    window.location.href = "/login";
  }
  return res;
}

// Auth
export async function login(email: string, password: string) {
  const form = new URLSearchParams();
  form.append("username", email);
  form.append("password", password);
  const res = await fetch(`${API_URL}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form,
  });
  if (!res.ok) throw new Error("Invalid credentials");
  const data = await res.json();
  localStorage.setItem("token", data.access_token);
  return data;
}

export async function getMe() {
  const res = await request("/api/auth/me");
  if (!res.ok) throw new Error("Not authenticated");
  return res.json();
}

export function logout() {
  localStorage.removeItem("token");
  window.location.href = "/login";
}

// Jobs
export async function createJob(data: {
  title: string;
  mega_link: string;
  excluded_files: string[];
  storage_target: string;
}) {
  const res = await request("/api/jobs/", {
    method: "POST",
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to create job");
  }
  return res.json();
}

export async function listJobs() {
  const res = await request("/api/jobs/");
  if (!res.ok) throw new Error("Failed to list jobs");
  return res.json();
}

export async function getJob(id: number) {
  const res = await request(`/api/jobs/${id}`);
  if (!res.ok) throw new Error("Failed to get job");
  return res.json();
}

export async function startJob(id: number) {
  const res = await request(`/api/jobs/${id}/start`, { method: "POST" });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to start job");
  }
  return res.json();
}

export async function deleteJob(id: number) {
  const res = await request(`/api/jobs/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error("Failed to delete job");
  return res.json();
}

export async function retryJob(id: number) {
  const res = await request(`/api/jobs/${id}/retry`, { method: "POST" });
  if (!res.ok) throw new Error("Failed to retry job");
  return res.json();
}

// Members
export async function createMember(email: string, password: string) {
  const res = await request("/api/members/", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to create member");
  }
  return res.json();
}

export async function listMembers() {
  const res = await request("/api/members/");
  if (!res.ok) throw new Error("Failed to list members");
  return res.json();
}

export async function updateMember(id: number, data: { is_active?: boolean; max_downloads_per_day?: number }) {
  const res = await request(`/api/members/${id}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to update member");
  return res.json();
}

export async function deleteMember(id: number) {
  const res = await request(`/api/members/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error("Failed to delete member");
  return res.json();
}

export async function grantAccess(userId: number, jobId: number) {
  const res = await request("/api/members/access", {
    method: "POST",
    body: JSON.stringify({ user_id: userId, job_id: jobId }),
  });
  if (!res.ok) throw new Error("Failed to grant access");
  return res.json();
}

export async function revokeAccess(userId: number, jobId: number) {
  const res = await request(`/api/members/access/${userId}/${jobId}`, { method: "DELETE" });
  if (!res.ok) throw new Error("Failed to revoke access");
  return res.json();
}

export async function grantAllAccess(jobId: number) {
  const res = await request(`/api/members/grant-all/${jobId}`, { method: "POST" });
  if (!res.ok) throw new Error("Failed to grant access");
  return res.json();
}

// Portal
export async function getMyContent() {
  const res = await request("/api/portal/my-content");
  if (!res.ok) throw new Error("Failed to get content");
  return res.json();
}

export async function getContentFolder(slug: string) {
  const res = await request(`/api/portal/content/${slug}`);
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to get content");
  }
  return res.json();
}

export async function getDownloadUrl(fileId: number) {
  const res = await request(`/api/portal/download/${fileId}`);
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to get download URL");
  }
  return res.json();
}
